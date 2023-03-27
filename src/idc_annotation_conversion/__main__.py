from io import BytesIO, BufferedReader
from itertools import islice
import logging
from pathlib import Path
import os
import tarfile
from typing import List, Generator, Optional

import click
from google.cloud import bigquery
from google.cloud import storage
import pydicom

from idc_annotation_conversion import cloud_config, cloud_io
from idc_annotation_conversion.convert import convert_annotations


COLLECTIONS = [
    "blca_polygon",
    "brca_polygon",
    "cesc_polygon",
    "coad_polygon",
    "gbm_polygon",
    "luad_polygon",
    "lusc_polygon",
    "paad_polygon",
    "prad_polygon",
    "read_polygon",
    "skcm_polygon",
    "stad_polygon",
    "ucec_polygon",
    "uvm_polygon",
]


def iter_csvs(ann_blob: storage.Blob) -> Generator[BufferedReader, None, None]:
    """Iterate over individual CSV files in a loaded tar.gz file.

    Parameters:
    -----------
    ann_bytes: google.cloud.storage.Blob
        Blob object for the annotation tar.gz file.

    Yields:
    -------
    io.BufferedReader
        Buffer for each CSV file in the original tar. This buffer can be
        used to load the contents of the CSV from the in-memory tar file.

    """
    # Download the annotation
    ann_bytes = ann_blob.download_as_bytes()

    # Untar in memory and yield each csv as a buffer
    with tarfile.open(fileobj=BytesIO(ann_bytes), mode='r:gz') as tar:
        for member in tar.getmembers():
            if member.isfile():
                yield tar.extractfile(member)


@click.command()
@click.option(
    "-c",
    "--collections",
    multiple=True,
    type=click.Choice(COLLECTIONS),
    help="Collections to use, all by default.",
    show_choices=True,
)
@click.option(
    "-n",
    "--number",
    type=int,
    help="Number to process per collection. All by default.",
)
@click.option(
    "-o",
    "--output-dir",
    type=click.Path(path_type=Path, file_okay=False),
    help="Output directory",
)
@click.option(
    "-b",
    "--output-bucket",
    help="Output bucket",
    default=cloud_config.DEFAULT_OUTPUT_BUCKET,
)
@click.option(
    "-p",
    "--output-prefix",
    help="Prefix for all output blobs",
)
def run(
    collections: Optional[List[str]],
    number: Optional[int] = None,
    output_dir: Optional[Path] = None,
    output_bucket: Optional[str] = None,
    output_prefix: Optional[str] = None,
):
    # Use all collections if none specified
    collections = collections or COLLECTIONS
    output_prefix = output_prefix or ""

    logging.basicConfig(level=logging.INFO)

    # Setup project and authenticate
    os.environ["GCP_PROJECT_ID"] = cloud_config.GCP_PROJECT_ID

    # Access bucket containing annotations
    storage_client = storage.Client(project=cloud_config.GCP_PROJECT_ID)
    ann_bucket = storage_client.bucket(cloud_config.ANNOTATION_BUCKET)
    public_bucket = storage_client.bucket(cloud_config.DICOM_IMAGES_BUCKET)

    # Setup bigquery client
    bq_client = bigquery.Client(cloud_config.GCP_PROJECT_ID)

    # Create output directory
    if output_dir is not None:
        output_dir.mkdir(exist_ok=True)

    # Loop over requested collections
    for collection in collections:
        prefix = f'cnn-nuclear-segmentations-2019/data-files/{collection}/'

        if output_dir is not None:
            collection_dir = output_dir / "collection"
            collection_dir.mkdir(exist_ok=True)

        # Loop over annotations in the bucket for this collection
        for ann_blob in islice(ann_bucket.list_blobs(prefix=prefix), number):
            if not ann_blob.name.endswith('.svs.tar.gz'):
                continue

            # Massage the blob name to derive container information
            # eg TCGA-05-4244-01Z-00-DX1.d4ff32cd-38cf-40ea-8213-45c2b100ac01
            filename = (
                ann_blob.name
                .replace(prefix, '')
                .split('/')[0]
                .replace('.svs.tar.gz', '')
            )

            # eg TCGA-05-4244-01Z-00-DX1, d4ff32cd-38cf-40ea-8213-45c2b100ac01
            container_id, crdc_instance_uuid = filename.split('.')

            logging.info(f"Processing container: {container_id}")

            selection_query = f"""
                SELECT
                    crdc_instance_uuid,
                    Cast(NumberOfFrames AS int) AS NumberOfFrames
                FROM
                    bigquery-public-data.idc_current.dicom_all
                WHERE
                    ContainerIdentifier='{container_id}'
                ORDER BY
                    NumberOfFrames
            """
            selection_result = bq_client.query(selection_query)
            selection_df = selection_result.result().to_dataframe()

            # Choose the instance uid as one with most frames (highest res)
            ins_uuid = selection_df.crdc_instance_uuid.iloc[-1]

            # for i, uuid in enumerate(selection_df.crdc_instance_uuid):
            #     dcm_blob = public_bucket.get_blob(f'{uuid}.dcm')
            #     dcm_bytes = dcm_blob.download_as_bytes()
            #     dcm_meta = pydicom.dcmread(
            #         BytesIO(dcm_bytes),
            #     )
            #     dcm_meta.save_as(f"outputs/{container_id}_im_{i}.dcm")
            # break

            # Download the DICOM file and load metadata only
            dcm_meta = cloud_io.read_dataset_from_blob(
                bucket=public_bucket,
                blob_name=f'{ins_uuid}.dcm',
                stop_before_pixels=True,
            )

            ann_dcm, seg_dcm = convert_annotations(
                annotation_csvs=iter_csvs(ann_blob),
                source_image_metadata=dcm_meta,
            )

            # Store objects to bucket
            if output_bucket is not None:
                output_bucket_obj = storage_client.bucket(output_bucket)
                blob_root = "" if output_prefix is None else f"{output_prefix}/"
                ann_blob_name = (
                    f"{blob_root}{collection}/{container_id}_ann.dcm"
                )
                seg_blob_name = (
                    f"{blob_root}{collection}/{container_id}_seg.dcm"
                )

                logging.info(f"Uploading annotation to {ann_blob_name}.")
                cloud_io.write_dataset_to_blob(
                    ann_dcm,
                    output_bucket_obj,
                    ann_blob_name,
                )
                logging.info(f"Uploading segmentation to {seg_blob_name}.")
                cloud_io.write_dataset_to_blob(
                    seg_dcm,
                    output_bucket_obj,
                    seg_blob_name,
                )

            # Store objects to filesystem
            if output_dir is not None:
                ann_path = collection_dir / f"{container_id}_ann.dcm"
                seg_path = collection_dir / f"{container_id}_seg.dcm"

                logging.info(f"Writing annotation to {str(ann_path)}.")
                ann_dcm.save_as(ann_path)

                logging.info(f"Writing segmentation to {str(seg_path)}.")
                seg_dcm.save_as(seg_path)


if __name__ == "__main__":
    run()