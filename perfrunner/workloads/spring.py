from perfrunner.helpers.remote import RemoteHelper
import logger

class Spring(object):
    def __init__(self, settings):
        logger.info("Initializing spring class")
        self.settings = settings
        self.remote = settings.remote

    def run(self):
        load_settings = self.settings
        creates = load_settings.creates
        reads = load_settings.reads
        updates = load_settings.updates
        deletes = load_settings.deletes
        expires = load_settings.expiration
        operations = load_settings.items
        throughput = int(load_settings.throughput) if load_settings.throughput != float('inf') \
            else load_settings.throughput
        size = load_settings.size
        existing_items = load_settings.existing_items
        items_in_working_set = int(load_settings.working_set)
        operations_to_hit_working_set = load_settings.working_set_access
        workers = load_settings.spring_workers
        logger.info("Inside Spring class to run workload gen via celery. {}".format(load_settings))
        self.remote.run_spring_on_kv(creates=creates, reads=reads, updates=updates, deletes=deletes,
                                     expires=expires, operations=operations, throughput=throughput, size=size,
                                     existing_items=existing_items, items_in_working_set=items_in_working_set,
                                     operations_to_hit_working_set=operations_to_hit_working_set, workers=workers)
