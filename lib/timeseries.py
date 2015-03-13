# Copyright © Los Alamos National Security, LLC, and others.

'''Functionality for reading and manipulating time series vectors.

   We refer to "shards" in the namespace/name dimension and "fragments" in the
   time dimenstion. That is, two time series with different namespaces and/or
   names might fall in different shards, and different elements in the same
   time series might fall in different fragments, but all elements in the same
   time series will be in the same shard, and all elements for the same time
   will be in the same fragment. Fragments are tagged with arbitrary,
   contiguously ordered strings and shards are tagged with contiguous integers
   from 0 to a maximum specified at open time.

   Currently, time series must be hourly (i.e., one element per hour),
   start/end on month boundaries, and have fragments equal to calendar months.
   I have attempted to make the API extensible to remove this limitation
   without disrupting existing code.'''

# FIXME to document
#
# - namespace cannot contain space character
# - when to create indexes
# - update docs for installing apsw
#   - installing at system level invisible in virtualenv)
#   - http://rogerbinns.github.io/apsw/download.html#easy-install-pip-pypi
#   - unzip apsw-3.8.8.2-r1.zip
#   - cd apsw-3.8.8.2-r1
#   - python setup.py fetch --all build --enable-all-extensions install test
#   - --missing-checksum-ok after --all
#   - tests take a while (5-10 minutes), omit if you want to live dangerously; but you can import the module and keep going while the tests run
# - document reason for no setting complete vector

import datetime
import enum
import os
import os.path
import sys

import numpy as np

import db
import hash_
import testable
import time_
import u

l = u.l
u.logging_init('test', verbose_=True)
l.debug('')


# Storage schema version
SCHEMA_VERSION = 1

# If a time series fragment is less than or equal to this, then the vector is
# stored compressed. The reasoning is to avoid wasting space on shards that
# are almost all zeros. In principle this can be changed on a case by cases
# basis, but the API does not currently support that.
#
# The value here is a random guess and is not supported by evidence.
FRAGMENT_TOTAL_ZMAX = 2

# Default data type
TYPE_DEFAULT = np.float32

# Which hash algorithm to use?
HASH = 'fnv1a_32'
hashf = getattr(hash_, HASH)

class Fragment_Source(enum.Enum):
   'Where did a fragment come from?'
   N = 1; NEW = 1           # created from scratch
   U = 2; UNCOMPRESSED = 2  # retrieved without compression from the database
   Z = 3; COMPRESSED = 3    # decompressed from the database


class Dataset(object):

   __slots__ = ('filename',
                'hashmod')

   def __init__(self, filename, hashmod):
      self.filename = filename
      self.hashmod = hashmod

   def dump(self):
      for sid in range(self.hashmod):
         for ts in self.fetch_all():
            print(ts)

   def fetch_all(self):
      return list()  # FIXME

   def open_month(self, month, writeable=False):
      if (month.day != 1):
         raise ValueError('month must have day=1, not %d' % month.day)
      if (hasattr(month, 'hour') and (   month.hour != 0
                                      or month.minute != 0
                                      or month.second != 0
                                      or month.microsecond != 0)):
         raise ValueError('month must have all sub-day attributes equal to zero')
      f = Fragment_Group(self, self.filename, time_.iso8601_date(month),
                         time_.hours_in_month(month))
      f.open(writeable)
      return f

   def shard(self, namespace, name):
      return hashf(namespace + '/' + name) % self.hashmod


# fetch(namespace, name) - error if nonexistent
# fetch_all(shard_id)
# fragment_tags_all()


class Fragment_Group(object):

   __slots__ = ('curs',
                'dataset',
                'db',
                'filename',
                'length',
                'metadata',
                'tag',
                'writeable')

   def __init__(self, dataset, filename, tag, length):
      self.dataset = dataset
      self.filename = '%s/%s.db' % (filename, tag)
      self.tag = tag
      self.length = length
      self.metadata = { 'fragment_total_zmax': FRAGMENT_TOTAL_ZMAX,
                        'hash': HASH,
                        'hashmod': self.dataset.hashmod,
                        'length': self.length,
                        'schema_version': SCHEMA_VERSION }

   def begin(self):
      self.db.begin()

   def close(self):
      self.create_indexes()
      self.db.close()
      self.writeable = None

   def commit(self):
      self.db.commit()

   def connect(self, writeable):
      self.writeable = writeable
      if (writeable):
         os.makedirs(os.path.dirname(self.filename), exist_ok=True)
      self.db = db.SQLite(self.filename, writeable)

   def create(self, namespace, name, dtype=TYPE_DEFAULT):
      'Create and return a fragment initialized to zero.'
      return Fragment(self, namespace, name, np.zeros(self.length, dtype=dtype),
                      Fragment_Source.NEW)

   def create_indexes(self):
      pass

   def fetch(self, namespace, name):
      (dtype, total, data) \
         = self.db.get_one(("""SELECT dtype, total, data FROM data%d
                              WHERE namespace=? AND name=?"""
                            % self.dataset.shard(namespace, name)),
                           (namespace, name))
      ar = np.frombuffer(data, dtype=dtype)
      f = Fragment(self, namespace, name, ar, Fragment_Source.UNCOMPRESSED)
      f.total = total
      return f

   def fetch_or_create(self, namespace, name, dtype=TYPE_DEFAULT):
      '''dtype is only used on create; if fetch is successful, the fragment is
         returned unchanged.'''
      try:
         return self.fetch(namespace, name)
      except db.Not_Enough_Rows_Error:
         return self.create(namespace, name, dtype)

   def initialize_db(self):
      if (not self.writeable):
         l.debug('not writeable, skipping init')
         return
      if (self.db.exists('sqlite_master', "type='table' AND name='metadata'")):
         l.debug('metadata table exists, skipping init')
         return
      l.debug('initializing')
      self.db.sql("""PRAGMA encoding='UTF-8';
                     PRAGMA page_size = 65536; """)
      self.db.begin()
      self.db.sql("""CREATE TABLE metadata (
                        key    TEXT NOT NULL PRIMARY KEY,
                        value  TEXT NOT NULL )""")
      self.db.sql_many("INSERT INTO metadata VALUES (?, ?)",
                       self.metadata.items())
      for i in range(self.dataset.hashmod):
         self.db.sql("""CREATE TABLE data%d (
                           namespace  TEXT NOT NULL,
                           name       TEXT NOT NULL,
                           dtype      TEXT NOT NULL,
                           total      REAL NOT NULL,
                           data       BLOB NOT NULL) """ % i)
      self.db.commit()

   def open(self, writeable):
      l.debug('opening %s, writeable=%s' % (self.filename, writeable))
      self.connect(writeable)
      # We use journal_mode = PERSIST to avoid metadata operations and
      # re-allocation, which can be expensive on parallel filesystems.
      self.db.sql("""PRAGMA cache_size = -1048576;
                     PRAGMA journal_mode = PERSIST;
                     PRAGMA synchronous = OFF; """)
      self.initialize_db()
      self.validate_db()

   def validate_db(self):
      db_meta = dict(self.db.get('SELECT key, value FROM metadata'))
      for (k, v) in self.metadata.items():
         if (str(v) != db_meta[k]):
            raise ValueError('Metadata mismatch at key %s: expected %s, found %s'
                             % (k, v, db_meta[k]))
      if (set(db_meta.keys()) != set(self.metadata.keys())):
         raise ValueError('Metadata mismatch: key sets differ')
      l.debug('validated %d metadata items' % len(self.metadata))

# compact(minimum)
# create_fragment(namespace, name)
# fetch_or_create_fragment(namespace, name)


class Fragment(object):

   __slots__ = ('data',      # time series vector fragment itself
                'group',
                'namespace',
                'name',
                'source',    # where the fragment came from
                'total')     # total of data (not updated when data changes)

   def __init__(self, group, namespace, name, data, source):
      self.group = group
      self.namespace = namespace
      self.name = name
      self.data = data
      self.source = source
      self.total = 0.0

   @property
   def shard(self):
      return self.group.dataset.shard(self.namespace, self.name)

   def __repr__(self):
      'Mostly for testing; output is inefficient for non-sparse fragments.'
      return '%s/%s %s %s %s' % (self.namespace, self.name,
                                 self.source.name, self.total,
                                 [i for i in enumerate(self.data) if i[1] != 0])

   def save(self):
      self.total_update()
      data = self.data.data
      if (self.source == Fragment_Source.NEW):
         self.group.db.sql("""INSERT INTO data%d
                                     (namespace, name, dtype, total, data)
                              VALUES (?, ?, ?, ?, ?)""" % self.shard,
                           (self.namespace, self.name, self.data.dtype.char,
                            self.total, self.data))
      else:
         self.group.db.sql("""UPDATE data%d
                              SET dtype=?, total=?, data=?
                              WHERE namespace=? AND name=?""" % self.shard,
                           (self.data.dtype.char, self.total, data,
                            self.namespace, self.name))

   def total_update(self):
      # np.sum() returns a NumPy data type, which confuses SQLite somehow.
      # Therefore, use a plain Python float. Also np.sum() is about 30 times
      # faster than Python sum() on a 1000-element array.
      self.total = float(self.data.sum())


testable.register('''

# Tests not implemented
#
# - DB does not validate
#   - missing metadata or data tables
#   - table columns do not match
#   - wrong number of data tables (too few, too many)
#   - metadata does not match (items differ, different number of items)
# - timing of create_indexes()
# - duplicate namespace/name pair
# - save already existing fragment (can fail at save or create_index time)

>>> import pytz
>>> tmp = os.environ['TMPDIR']
>>> january = time_.iso8601_parse('2015-01-01')
>>> february = time_.iso8601_parse('2015-02-01')
>>> january_nonutc = datetime.datetime(2015, 1, 1, tzinfo=pytz.timezone('GMT'))
>>> mid_january1 = time_.iso8601_parse('2015-01-01 00:00:01')
>>> mid_january2 = time_.iso8601_parse('2015-01-02')

# create some data over 2 months
#
#   ns     name    description
#   -----  ----    -----------
#
#   aboth  abov11  above threshold in both months
#   bboth  abov00  below threshold in both months
#   full   abov11  above threshold in both months
#   full   abov01  above threshold in second month only
#   full   abov10  above threshold in first month only
#   full   abov00  below threshold in both months

# Create time series dataset
>>> d = Dataset(tmp + '/foo', 4)

# Try to open some bogus months
>>> jan = d.open_month(january_nonutc, writeable=True)
Traceback (most recent call last):
  ...
ValueError: time zone must be UTC
>>> jan = d.open_month(mid_january1, writeable=True)
Traceback (most recent call last):
  ...
ValueError: month must have all sub-day attributes equal to zero
>>> jan = d.open_month(mid_january2, writeable=True)
Traceback (most recent call last):
  ...
ValueError: month must have day=1, not 2

# Really open; should be empty so far
>>> jan = d.open_month(january, writeable=True)

#>>> feb = d.open_month(february, writeable=True)
#>>> d.dump()

# Add first time series
>>> jan.begin()
>>> a = jan.create('aboth', 'abov11')
>>> a
aboth/abov11 N 0.0 []
>>> a.data[0] = 11
>>> a
aboth/abov11 N 0.0 [(0, 11.0)]
>>> a.data[2] = 22.0
>>> a
aboth/abov11 N 0.0 [(0, 11.0), (2, 22.0)]
>>> a.save()
>>> jan.commit()

# Try some fetching
>>> jan.fetch('aboth', 'abov11')
aboth/abov11 U 33.0 [(0, 11.0), (2, 22.0)]
>>> jan.fetch('aboth', 'nonexistent')
Traceback (most recent call last):
  ...
db.Not_Enough_Rows_Error: no such row
>>> jan.fetch('nonexistent', 'abov11')
Traceback (most recent call last):
  ...
db.Not_Enough_Rows_Error: no such row
>>> jan.fetch_or_create('aboth', 'abov11')
aboth/abov11 U 33.0 [(0, 11.0), (2, 22.0)]
>>> jan.fetch_or_create('aboth', 'nonexistent')
aboth/nonexistent N 0.0 []

# Add remaining time series

# Compact

# Close
>>> jan.close()

# different data type
# create time series that would be compressed
# update time series
#   uncompressed -> uncompressed
#   uncompressed -> compressed
#   compressed -> compressed
#   compressed -> uncompressed
# test commit visibility to readers
# auto-prune save()
# close and re-open

''')
