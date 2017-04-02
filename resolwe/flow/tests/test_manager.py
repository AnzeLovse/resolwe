# pylint: disable=missing-docstring
from __future__ import absolute_import, division, print_function, unicode_literals

import os

from django.contrib.contenttypes.models import ContentType

from resolwe.flow.models import Data, Process
from resolwe.test import TransactionProcessTestCase

PROCESSES_DIR = os.path.join(os.path.dirname(__file__), 'processes')


# NOTE: Manager is triggered on the commit of the transastion. Because
#       of this it should be tested in TransactionTestCase, as it won't
#       be triggered if whole test is wrapped in a transaction.
class TestManager(TransactionProcessTestCase):

    def _pre_setup(self, *args, **kwargs):
        # NOTE: This is a work-around for Django issue #10827
        # (https://code.djangoproject.com/ticket/10827) that clears the
        # ContentType cache before permissions are setup.
        ContentType.objects.clear_cache()
        super(TestManager, self)._pre_setup(*args, **kwargs)

    def setUp(self):
        super(TestManager, self).setUp()

        self._register_schemas(path=[PROCESSES_DIR])

    def test_create_data(self):
        """Test that manager is run when new object is created."""
        process = Process.objects.filter(slug='test-min').latest()
        data = Data.objects.create(
            name='Test data',
            contributor=self.contributor,
            process=process,
        )

        data.refresh_from_db()
        self.assertEqual(data.status, Data.STATUS_DONE)

    def test_spawned_process(self):
        """Test that manager is run for spawned processes."""
        process = Process.objects.filter(slug='test-spawn-new').latest()
        Data.objects.create(
            name='Test data',
            contributor=self.contributor,
            process=process,
        )

        # Created and spawned objects should be done.
        self.assertEqual(Data.objects.filter(status=Data.STATUS_DONE).count(), 2)
