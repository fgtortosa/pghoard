"""
pghoard

Copyright (c) 2015 Ohmu Ltd
See LICENSE for details
"""
from pghoard.common import Empty
from pghoard.errors import FileNotFoundFromStorageError, InvalidConfigurationError
from threading import Thread
import logging
import os
import shutil
import time


def get_object_storage_transfer(key, value):
    if key == "azure":
        from . azure import AzureTransfer
        storage = AzureTransfer(value["account_name"], value["account_key"], value.get("container_name", "pghoard"))
    elif key == "google":
        from . google import GoogleTransfer
        storage = GoogleTransfer(project_id=value["project_id"],
                                 bucket_name=value.get("bucket_name", "pghoard"),
                                 credential_file=value.get("credential_file"))
    elif key == "s3":
        from . s3 import S3Transfer
        storage = S3Transfer(value["aws_access_key_id"], value["aws_secret_access_key"],
                             value.get("region", ""), value['bucket_name'],
                             host=value.get("host"), port=value.get("port"), is_secure=value.get("is_secure", False))
    else:
        raise InvalidConfigurationError("unknown storage type {0!r}".format(key))
    return storage


class TransferAgent(Thread):
    def __init__(self, config, compression_queue, transfer_queue):
        Thread.__init__(self)
        self.log = logging.getLogger("TransferAgent")
        self.config = config
        self.compression_queue = compression_queue
        self.transfer_queue = transfer_queue
        self.running = True
        self.state = {}
        self.site_transfers = {}
        self.log.debug("TransferAgent initialized")

    def set_state_defaults_for_site(self, site):
        if site not in self.state:
            EMPTY = {"data": 0, "count": 0, "time_taken": 0.0, "failures": 0}
            self.state[site] = {
                "upload": {"basebackup": EMPTY.copy(), "xlog": EMPTY.copy(), "timeline": EMPTY.copy()},
                "download": {"basebackup": EMPTY.copy(), "xlog": EMPTY.copy(), "timeline": EMPTY.copy()},
            }

    def get_object_storage(self, site_name):
        storage = self.site_transfers.get(site_name)
        if not storage:
            cfg = self.config["backup_sites"][site_name].get("object_storage", {})
            for key, value in cfg.items():
                storage = get_object_storage_transfer(key, value)
                self.site_transfers[site_name] = storage
        return storage

    def form_key_path(self, file_to_transfer):
        if file_to_transfer["filetype"] == "basebackup":
            name = os.path.basename(os.path.dirname(file_to_transfer["local_path"]))
        else:
            name = os.path.splitext(os.path.basename(file_to_transfer["local_path"]))[0]
        key = "/".join([file_to_transfer["site"], file_to_transfer["filetype"], name])
        return key

    def run(self):
        while self.running:
            try:
                file_to_transfer = self.transfer_queue.get(timeout=1.0)
            except Empty:
                continue
            if file_to_transfer["type"] == "QUIT":
                break
            self.log.debug("Starting to %r %r, size: %r",
                           file_to_transfer["type"], file_to_transfer["local_path"],
                           file_to_transfer.get("file_size", "unknown"))
            start_time = time.time()
            key = self.form_key_path(file_to_transfer)
            oper = file_to_transfer["type"].lower()
            oper_func = getattr(self, "handle_" + oper, None)
            if oper_func is None:
                self.log.warning("Invalid operation %r", file_to_transfer["type"])
                continue
            site = file_to_transfer["site"]
            filetype = file_to_transfer["filetype"]

            result = oper_func(site, key, file_to_transfer)

            # increment statistics counters
            self.set_state_defaults_for_site(site)
            oper_size = file_to_transfer.get("file_size", 0)
            if result["success"]:
                self.state[site][oper][filetype]["count"] += 1
                self.state[site][oper][filetype]["data"] += oper_size
                self.state[site][oper][filetype]["time_taken"] += time.time() - start_time
            else:
                self.state[site][oper][filetype]["failures"] += 1

            # push result to callback_queue if provided
            if result.get("call_callback", True) and "callback_queue" in file_to_transfer:
                file_to_transfer["callback_queue"].put(result)

            self.log.info("%r %stransfer of key: %r, size: %r, took %.3fs",
                          file_to_transfer["type"],
                          "FAILED " if not result["success"] else "",
                          key, oper_size, time.time() - start_time)

        self.log.info("Quitting TransferAgent")

    def handle_download(self, site, key, file_to_transfer):
        try:
            storage = self.get_object_storage(site)

            content, metadata = storage.get_contents_to_string(key)
            file_to_transfer["file_size"] = len(content)
            # Note that here we flip the local_path to mean the target_path
            self.compression_queue.put({
                "blob": content,
                "callback_queue": file_to_transfer["callback_queue"],
                "local_path": file_to_transfer["target_path"],
                "metadata": metadata,
                "site": site,
                "type": "DECOMPRESSION",
            })
            return {"success": True, "call_callback": False}
        except Exception as ex:  # pylint: disable=broad-except
            if isinstance(ex, FileNotFoundFromStorageError):
                self.log.warning("%r not found from storage", key)
            else:
                self.log.exception("Problem happened when downloading: %r, %r", key, file_to_transfer)
            return {"success": False}

    def handle_upload(self, site, key, file_to_transfer):
        try:
            storage = self.get_object_storage(site)
            if "blob" in file_to_transfer:
                storage.store_file_from_memory(key, file_to_transfer["blob"],
                                               metadata=file_to_transfer["metadata"])
            else:
                storage.store_file_from_disk(key, file_to_transfer["local_path"],
                                             metadata=file_to_transfer["metadata"])
                try:
                    if file_to_transfer["filetype"] == "basebackup":
                        self.log.debug("Deleting directory path: %r", os.path.dirname(file_to_transfer["local_path"]))
                        shutil.rmtree(os.path.dirname(file_to_transfer["local_path"]))
                    else:
                        self.log.debug("Deleting file: %r since it has been uploaded", file_to_transfer["local_path"])
                        os.unlink(file_to_transfer["local_path"])
                        metadata_path = file_to_transfer["local_path"] + ".metadata"
                        if os.path.exists(metadata_path):
                            os.unlink(metadata_path)
                except:  # pylint: disable=bare-except
                    self.log.exception("Problem in deleting file: %r", file_to_transfer["local_path"])
            return {"success": True}
        except Exception:  # pylint: disable=broad-except
            self.log.exception("Problem in moving file: %r, need to retry", file_to_transfer["local_path"])
            # TODO come up with something so we don't busy loop
            time.sleep(0.5)
            self.transfer_queue.put(file_to_transfer)
            return {"success": False, "call_callback": False}
