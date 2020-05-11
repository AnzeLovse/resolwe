"""Data transfer between connectors."""
import concurrent.futures
import logging
from contextlib import suppress
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, List, Optional

import wrapt
from requests.exceptions import ConnectionError as RequestsConnectionError
from requests.exceptions import ReadTimeout

from .circular_buffer import CircularBuffer
from .exceptions import DataTransferError
from .hasher import StreamHasher

if TYPE_CHECKING:
    from .baseconnector import BaseStorageConnector

try:
    from google.api_core.exceptions import ServiceUnavailable

    gcs_exceptions = [ServiceUnavailable]
except ModuleNotFoundError:
    gcs_exceptions = []


logger = logging.getLogger(__name__)
ERROR_MAX_RETRIES = 3
transfer_exceptions = tuple(
    gcs_exceptions + [DataTransferError] + [RequestsConnectionError, ReadTimeout]
)


@wrapt.decorator
def retry_on_transfer_error(wrapped, instance, args, kwargs):
    """Retry on tranfser error."""
    for _ in range(ERROR_MAX_RETRIES):
        try:
            return wrapped(*args, **kwargs)

        except transfer_exceptions as err:
            connection_err = err

    raise connection_err


class Transfer:
    """Transfer data between two storage connectors using in-memory buffer."""

    def __init__(
        self,
        from_connector: "BaseStorageConnector",
        to_connector: "BaseStorageConnector",
    ):
        """Initialize transfer object."""
        self.from_connector = from_connector
        self.to_connector = to_connector

    def pre_processing(self, url: str, objects: Optional[List[str]] = None):
        """Notify connectors that transfer is about to start.

        The connector is allowed to change names of the objects that are to be
        transfered. This allows us to do some pre-processing, like zipping all
        files into one and transfering that one.

        :param url: base url for file transfer.

        :param objects: list of objects to be transfered, their paths are
            relative with respect to the url.
        """
        objects_to_transfer = self.from_connector.before_get(objects, url)
        self.to_connector.before_push(objects_to_transfer, url)
        return objects_to_transfer

    def post_processing(self, url: str, objects: Optional[List[str]] = None):
        """Notify connectors that transfer is complete.

        :param url: base url for file transfer.

        :param objects: the list ob objects that was actually transfered.The
            paths are relative with respect to the url.
        """
        self.from_connector.after_get(objects, url)
        objects_stored = self.to_connector.after_push(objects, url)
        return objects_stored

    def transfer_rec(self, url: str, objects: Optional[List[str]] = None):
        """Transfer all objects under the given URL.

        Objects are read from to_connector and copied to from_connector. This
        could cause significant number of operations to a storage provider
        since it could lists all the objects in the url.

        :param url: the given URL to transfer from/to.

        :param objects: the list of objects to transfer. Their paths are
            relative with respect to the url. When the argument is not given a
            list of objects is obtained from the connector.
        """
        if objects is None:
            objects = self.from_connector.get_object_list(url)

        # Pre-processing.
        try:
            objects_to_transfer = self.pre_processing(url, objects)
        except Exception:
            logger.exception(
                "Error in pre-processing while transfering data from url {}".format(url)
            )
            raise DataTransferError()

        url = Path(url)
        for entry in objects:
            # Do not transfer directories.
            if not entry.endswith("/"):
                self.transfer(url / entry, url / entry)

        # Post-processing.
        try:
            objects_stored = self.post_processing(url, objects_to_transfer)
        except Exception:
            logger.exception(
                "Error in post-processing while transfering data from url {}".format(
                    url
                )
            )
            raise DataTransferError()

        return None if objects_stored is objects else objects_stored

    @retry_on_transfer_error
    def transfer(self, from_url: str, to_url: str):
        """Transfer single object between two storage connectors."""

        def future_done(stream_to_close, future):
            stream_to_close.close()
            if future.exception() is not None:
                executor.shutdown(wait=False)

        hash_stream = CircularBuffer()
        data_stream = CircularBuffer()
        hasher_chunk_size = 8 * 1024 * 1024

        hasher = StreamHasher(hasher_chunk_size)
        download_hash_type = self.from_connector.supported_download_hash[0]
        upload_hash_type = self.to_connector.supported_upload_hash[0]

        # Hack for S3/local connector to use same chunk size for transfer and
        # hash calculation (affects etag calculation).
        if hasattr(self.to_connector, "multipart_chunksize"):
            hasher_chunk_size = self.to_connector.multipart_chunksize

        # Check if file already exist and has the right hash.
        to_hashes = self.to_connector.supported_download_hash
        from_hashes = self.from_connector.supported_download_hash
        common_hash = [e for e in to_hashes if e in from_hashes]
        if common_hash:
            hash_type = common_hash[0]
            from_hash = self.from_connector.get_hash(from_url, hash_type)
            to_hash = self.to_connector.get_hash(to_url, hash_type)
            if from_hash == to_hash and from_hash is not None:
                # Object exists and has the right hash.
                logger.debug(
                    "From: {}:{}".format(self.from_connector.name, from_url)
                    + " to: {}:{}".format(self.to_connector.name, to_url)
                    + " object exists with right hash, skipping."
                )
                return

        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
            download_task = executor.submit(
                self.from_connector.get, from_url, hash_stream
            )
            hash_task = executor.submit(hasher.compute, hash_stream, data_stream)
            upload_task = executor.submit(self.to_connector.push, data_stream, to_url)
            download_task.add_done_callback(partial(future_done, hash_stream))
            hash_task.add_done_callback(partial(future_done, data_stream))
            futures = (download_task, hash_task, upload_task)

        if any(f.exception() is not None for f in futures):
            with suppress(Exception):
                self.to_connector.delete(to_url)
            ex = [f.exception() for f in futures if f.exception() is not None]
            messages = [str(e) for e in ex]
            raise DataTransferError("\n\n".join(messages))

        from_hash = self.from_connector.get_hash(from_url, download_hash_type)
        to_hash = self.to_connector.get_hash(to_url, upload_hash_type)

        hasher_from_hash = hasher.hexdigest(download_hash_type)
        hasher_to_hash = hasher.hexdigest(upload_hash_type)

        if (from_hash, to_hash) != (hasher_from_hash, hasher_to_hash):
            with suppress(Exception):
                self.to_connector.delete(to_url)
            raise DataTransferError()

        # Store computed hashes as metadata for later use.
        hashes = {
            hash_type: hasher.hexdigest(hash_type)
            for hash_type in StreamHasher.KNOWN_HASH_TYPES
        }
        # This is strictly speaking not a hash but is set to know the value
        # of upload_chunk_size for awss3etag computation.
        hashes["_upload_chunk_size"] = str(hasher.chunk_size)
        self.to_connector.set_hashes(to_url, hashes)
