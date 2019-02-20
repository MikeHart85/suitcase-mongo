import event_model
from pathlib import Path
#from ._version import get_versions
#from mongobox import MongoBox
from collections import defaultdict
from pymongo import MongoClient
from concurrent.futures import ThreadPoolExecutor
from pymongo import UpdateOne
from pymongo.errors import BulkWriteError
import threading
import sys
from time import sleep
import json
import pdb
#__version__ = get_versions()['version']
#del get_versions


class Serializer(event_model.DocumentRouter):
    # how to prevent the same run from being frozen two times?
    # How do we integrate with the run engine?

    def __init__(self, volatile_db, permanent_db, num_threads=1,
                   max_doc_size=5000000, **kwargs):
        """
        Insert documents into MongoDB using layout v2.

        Note that this Seralizer does not share the standard Serializer
        name or signature common to suitcase packages because it can only write
        via pymongo, not to an arbitrary user-provided buffer.
        """
        self._MAX_DOC_SIZE = max_doc_size
        self._permanent_db = permanent_db
        self._volatile_db = volatile_db
        self._event_buffer = DocBuffer('event', self._MAX_DOC_SIZE)
        self._datum_buffer = DocBuffer('datum', self._MAX_DOC_SIZE)
        #kwargs.setdefault('cls', NumpyEncoder)
        self._kwargs = kwargs
        self._start_found = False
        self._run_uid = None
        self._frozen = False

        self._executor = ThreadPoolExecutor(max_workers=num_threads)
        self._executor.submit(self._event_worker)
        self._executor.submit(self._datum_worker)

    def __call__(self, name, doc):
        if self._frozen:
            raise RuntimeError("Cannot insert documents into a "
                               "frozen Serializer")
        sanitized_doc = doc.copy()
        event_model.sanitize_doc(sanitized_doc)
        return super().__call__(name, sanitized_doc)

    def _event_worker(self):
        while not self._frozen:
            event_dump = self._event_buffer.dump()
            print(event_dump)
            self._bulkwrite_event(event_dump)
        print("event worker closed")

    def _datum_worker(self):
        while not self._frozen:
            datum_dump = self._datum_buffer.dump()
            self._bulkwrite_datum(datum_dump)
        print("datum worker closed")

    def start(self, doc):
        self._check_start(doc)
        self._run_uid = doc['uid']
        self._insert_header('start', doc)
        return doc

    def stop(self, doc):
        self._insert_header('stop', doc)
        self.close()
        return doc

    def descriptor(self, doc):
        self._insert_header('descriptors', doc)
        return doc

    def resource(self, doc):
        self._insert_header('resources', doc)
        return doc

    def event(self,doc):
        self._event_buffer.insert(doc)
        return doc

    def datum(self,doc):
        self._datum_buffer.insert(doc)
        return doc

    def event_page(self, doc):
        self._bulkwrite_event({doc['descriptor']:doc})
        return doc

    def datum_page(self, doc):
        self._bulkwrite_datum({doc['resource']:doc})
        return doc

    def close(self):
        self._freeze()

    def _freeze(self):
        self._frozen = True
        self._event_buffer.freeze()
        self._datum_buffer.freeze()
        self._executor.shutdown(wait=True)

        if self._event_buffer._current_size > 0:
            self._event_worker()
        if self._datum_buffer._current_size > 0:
            self._datum_worker()

        pdb.set_trace()

        volatile_run = self._get_run(self._volatile_db, self._run_uid)
        self._insert_run(self._permanent_db, volatile_run)
        permanent_run = self._get_run(self._permanent_db, self._run_uid)

        if  volatile_run != permanent_run:
            raise IOError("Failed to move data to permanent database.")
        else:
            self._volatile_db.header.drop()
            self._volatile_db.events.drop()
            self._volatile_db.datum.drop()

    def _get_run(self, db, run_uid):
        run = list()
        header = db.header.find_one({'run_id': run_uid}, {'_id' : False})

        if header == None:
            raise RuntimeError(f"Run not found {run_uid}")

        run.append(('header',header))

        if 'descriptors' in header.keys():
            for descriptor in header['descriptors']:
                run += [('event',doc) for doc in
                        db.events.find({'descriptor': descriptor}, \
                                        {'_id':False})]

        if 'resources' in header.keys():
            for resource in header['resources']:
                run += [('datum', doc) for doc in
                        db.datum.find({'resource': resource}, {'_id':False})]

        return run

    def _insert_run(self, db, run):
        for collection, doc in run:
            result = db[collection].insert_one(doc)
            del doc['_id']

    def _insert_header(self, name,  doc):
        self._volatile_db.header.update_one(
                        {'run_id': self._run_uid}, {'$push':
                        {name: doc}}, upsert=True)

    def _bulkwrite_datum(self, datum_buffer):
        operations = [self._updateone_datumpage(resource, datum)
                        for resource, datum in datum_buffer.items()]
        self._volatile_db.bulkrite(operations, ordered=False)

    def _bulkwrite_event(self, event_buffer):
        operations = [self._updateone_eventpage(descriptor, eventpage) \
                        for descriptor, eventpage in event_buffer.items()]
        print("operations: ", operations)
        self._volatile_db.events.bulkwrite(operations, ordered=False)

    def _updateone_eventpage(self, descriptor, eventpage):
        print("updateone_eventpage")
        event_size = sys.getsizeof(eventpage)

        data_string = {'data.' + key : value_array
                for key, value_array in eventpage['data'].items()}

        timestamp_string =  {'timestamps.' + key : value_array
                for key, value_array in eventpage['timestamps'].items()}

        filled_string = {'filled.' + key : value_array
                for key, value_array in eventpage['filled'].items()}

        update_string = {**data_string, **timestamp_string, **filled_string}

        return UpdateOne(
            {'descriptor': descriptor,'size': {'$lt': self._MAX_DOC_SIZE}},
            {'$pushall': {'uid': eventpage['uid'],
                          'time': eventpage['time'],
                          'seq_num': eventpage['seq_num'],
                          **update_string},
            '$inc': {'size':event_size}},
            upsert=True)

    def _updateone_datumpage(self, resource, datumpage):
        datum_size = sys.getsizeof(datumpage)

        kwargs_string = {'datum_kwargs.' + key : value_array
                for key, value_array in datumpage['datum_kwargs'].items()}

        return UpdateOne(
            {'resource': resource,'size': {'$lt': self._MAX_DOC_SIZE}},
            {'$pushall': {'datum_id': datumpage['uid'],
                          **kwargs_string},
            '$inc': {'size': datum_size}},
            upsert=True)

    def _check_start(self, doc):
        if self._start_found:
            raise RuntimeError(
                "The serializer in suitcase-mongo expects "
                "documents from one run only. Two `start` documents were "
                "received.")
        else:
            self._start_found = True

    def __repr__(self):
        # Display connection info in eval-able repr.
        return "temp repr" #f'{type(self).__name__}(run_uid={self._run_uid})'


class DocBuffer():

    """
    DocBuffer is a thread-safe "embedding" buffer for bluesky event or datum
    documents.

    "embedding" refers to combining multiple documents from a stream of
    documents into a single document, where the values of matching keys are
    stored as a list, or dictionary of lists.

    DocBuffer has two public methods, insert, and dump. Insert, embedes an
    event or datum document in the buffer and blocks if the buffer is full.
    Dump returns a reference to the buffer and creates a new buffer for new
    inserts. Dump blocks if the buffer is empty.

    Events with different descriptors, or datum with different resources are
    stored in separate embedded documents in the buffer. The buffer uses a
    defaultdict so new embedded documents are automatically created when they
    are needed. The dump method totally clears the buffer. This mechanism
    automatically manages the lifetime of the embeded documents in the buffer.

    The doc_type argument which can be either 'event' or 'datum'.
    The the details of the embedding differ for event and datum documents.
    """

    def __init__(self, doc_type, max_size):
        self._max_size = max_size
        self._current_size = 0
        self._doc_buffer = defaultdict(lambda: defaultdict(lambda:
                                                    defaultdict(list)))
        self._mutex = threading.Lock()
        self._not_full = threading.Condition(self._mutex)
        self._not_empty = threading.Condition(self._mutex)
        self._frozen = False

        if doc_type == "event":
            self._array_keys = set(["seq_num","time","uid"])
            self._dataframe_keys = set(["data","timestamps","filled"])
            self._stream_id_key = "descriptor"
        elif doc_type == "datum":
            self._array_keys = set(["datum_id"])
            self._dataframe_keys = set(["datum_kwargs"])
            self._stream_id_key = "resource"


    def insert(self, doc):
        print("insert start")
        with self._not_full:
            self._not_full.wait_for(lambda :
                                    self._current_size < self._max_size)
            print("insert insert")
            self._buffer_insert(doc)
            self._current_size += sys.getsizeof(doc)
            self._not_empty.notify()

    def dump(self):
        print("dump start")
        with self._not_empty:
            self._not_empty.wait_for(lambda: (
                                self._current_size or self._frozen))
            print("dump dump")
            event_buffer_dump = self._doc_buffer
            self._doc_buffer = defaultdict(lambda: defaultdict(lambda:
                                                        defaultdict(list)))
            self._current_size = 0
            self._not_full.notify()
            return event_buffer_dump

    def _buffer_insert(self, doc):
        for key, value in doc.items():
            if key in self._array_keys:
                self._doc_buffer[doc[self._stream_id_key]][key] = list(
                    self._doc_buffer[doc[self._stream_id_key]][key])
                self._doc_buffer[doc[self._stream_id_key]][key].append(value)
            elif key in self._dataframe_keys:
                for inner_key, inner_value in doc[key].items():
                    self._doc_buffer[doc[self._stream_id_key]][key][inner_key] \
                        .append(inner_value)
            else:
                self._doc_buffer[doc[self._stream_id_key]][key] = value

    def freeze(self):
        self._frozen = True
        self._mutex.acquire()
        self._not_empty.notify()
        self._mutex.release()