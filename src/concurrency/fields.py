from __future__ import absolute_import, unicode_literals
import time
import copy
import logging
from functools import update_wrapper
from django.utils.functional import cached_property
from django.utils.translation import ugettext_lazy as _
from django.db.models.fields import Field

try:
    from django.db.models.signals import class_prepared, post_migrate
except:
    from django.db.models.signals import class_prepared, post_syncdb as post_migrate

from concurrency import forms
from concurrency.config import conf
from concurrency.core import ConcurrencyOptions, _wrap_model_save
from concurrency.api import get_revision_of_object, disable_concurrency
from concurrency.utils import refetch

logger = logging.getLogger(__name__)

OFFSET = int(time.mktime((2000, 1, 1, 0, 0, 0, 0, 0, 0)))


def class_prepared_concurrency_handler(sender, **kwargs):
    if hasattr(sender, '_concurrencymeta'):
        origin = getattr(sender._concurrencymeta._base, '_concurrencymeta')
        local = copy.deepcopy(origin)
        setattr(sender, '_concurrencymeta', local)

        if hasattr(sender, 'ConcurrencyMeta'):
            sender._concurrencymeta.enabled = getattr(sender.ConcurrencyMeta, 'enabled')

        if not (sender._concurrencymeta._manually):
            _wrap_model_save(sender)

        setattr(sender, 'get_concurrency_version', get_revision_of_object)
    else:
        logger.debug('Skipped concurrency for %s' % sender)


def post_syncdb_concurrency_handler(sender, **kwargs):
    from concurrency.triggers import create_triggers
    from django.db import connections
    databases = [alias for alias in connections]
    create_triggers(databases)


class_prepared.connect(class_prepared_concurrency_handler, dispatch_uid='class_prepared_concurrency_handler')

_TRIGGERS = []
if not conf.MANUAL_TRIGGERS:
    post_migrate.connect(post_syncdb_concurrency_handler, dispatch_uid='post_syncdb_concurrency_handler')


class VersionField(Field):
    """ Base class """

    def __init__(self, **kwargs):
        self.manually = kwargs.pop('manually', False)

        verbose_name = kwargs.get('verbose_name', None)
        name = kwargs.get('name', None)
        db_tablespace = kwargs.get('db_tablespace', None)
        db_column = kwargs.get('db_column', None)
        help_text = kwargs.get('help_text', _('record revision number'))

        super(VersionField, self).__init__(verbose_name, name,
                                           help_text=help_text,
                                           default=0,
                                           db_tablespace=db_tablespace,
                                           db_column=db_column)

    # def deconstruct(self):
    #     name, path, args, kwargs = super(VersionField, self).deconstruct()
    #     del kwargs["max_length"]
    #     kwargs['default'] = 1
    #     return name, path, args, kwargs

    def get_default(self):
        return 0

    def get_internal_type(self):
        return "BigIntegerField"

    def to_python(self, value):
        return int(value)

    def validate(self, value, model_instance):
        pass

    def formfield(self, **kwargs):
        kwargs['form_class'] = self.form_class
        kwargs['widget'] = forms.VersionField.widget
        return super(VersionField, self).formfield(**kwargs)

    def contribute_to_class(self, cls, name, virtual_only=False):
        super(VersionField, self).contribute_to_class(cls, name)
        if hasattr(cls, '_concurrencymeta') or cls._meta.abstract:
            return
        setattr(cls, '_concurrencymeta', ConcurrencyOptions())
        cls._concurrencymeta._field = self
        cls._concurrencymeta._base = cls
        cls._concurrencymeta._manually = self.manually

    def _set_version_value(self, model_instance, value):
        setattr(model_instance, self.attname, int(value))

    def pre_save(self, model_instance, add):
        # if conf.PROTOCOL >= 2:
        if add:
            value = self._get_next_version(model_instance)
            self._set_version_value(model_instance, value)
        return getattr(model_instance, self.attname)
        # value = self._get_next_version(model_instance)
        # self._set_version_value(model_instance, value)
        # return value

    @staticmethod
    def _wrap_save(func):
        from concurrency.api import concurrency_check

        def inner(self, force_insert=False, force_update=False, using=None, **kwargs):
            if self._concurrencymeta.enabled and \
                    conf.ENABLED and \
                    not getattr(self, '_concurrency_disabled', False):
                concurrency_check(self, force_insert, force_update, using, **kwargs)
            return func(self, force_insert, force_update, using, **kwargs)

        return update_wrapper(inner, func)

    def _wrap_save_base(self, func):
        def _save_base(model_instance, raw=False, force_insert=False,
                       force_update=False, using=None, update_fields=None):
            if force_insert:
                with disable_concurrency(model_instance):
                    return func(model_instance, raw, force_insert, force_update, using, update_fields)
            return func(model_instance, raw, force_insert, force_update, using, update_fields)

        return update_wrapper(_save_base, func)

    def _wrap_do_update(self, func):

        def _do_update(model_instance, base_qs, using, pk_val, values, update_fields, forced_update):

            # if conf.PROTOCOL != 2:
            #     return func(model_instance, base_qs, using, pk_val, values, update_fields, forced_update)

            version_field = model_instance._concurrencymeta._field
            old_version = get_revision_of_object(model_instance)

            if not version_field.model._meta.abstract:
                if version_field.model is not base_qs.model:
                    return func(model_instance, base_qs, using, pk_val, values, update_fields, forced_update)

            for i, (field, _1, value) in enumerate(values):
                if field == version_field:
                    new_version = field._get_next_version(model_instance)
                    values[i] = (field, _1, new_version)
                    field._set_version_value(model_instance, new_version)
                    break
            if values:
                if model_instance._concurrencymeta.enabled and \
                        conf.ENABLED and \
                        not getattr(model_instance, '_concurrency_disabled', False) and \
                        old_version:
                    filter_kwargs = {'pk': pk_val, version_field.attname: old_version}
                    updated = base_qs.filter(**filter_kwargs)._update(values) >= 1
                    if not updated:
                        version_field._set_version_value(model_instance, old_version)
                        updated = conf._callback(model_instance)
                else:
                    filter_kwargs = {'pk': pk_val}
                    updated = base_qs.filter(**filter_kwargs)._update(values) >= 1
            else:
                updated = base_qs.filter(pk=pk_val).exists()

            return updated

        return update_wrapper(_do_update, func)


class IntegerVersionField(VersionField):
    """
        Version Field that returns a "unique" version number for the record.

        The version number is produced using time.time() * 1000000, to get the benefits
        of microsecond if the system clock provides them.

    """
    form_class = forms.VersionField

    def _get_next_version(self, model_instance):
        old_value = getattr(model_instance, self.attname, 0)
        return max(int(old_value) + 1, (int(time.time() * 1000000) - OFFSET))


class AutoIncVersionField(VersionField):
    """
        Version Field increment the revision number each commit

    """
    form_class = forms.VersionField

    def _get_next_version(self, model_instance):
        return int(getattr(model_instance, self.attname, 0)) + 1


class TriggerVersionField(VersionField):
    """
        Version Field increment the revision number each commit

    """
    form_class = forms.VersionField

    def __init__(self, *args, **kwargs):
        self._trigger_name = kwargs.pop('trigger_name', None)
        self._trigger_exists = False
        super(TriggerVersionField, self).__init__(*args, **kwargs)

    def contribute_to_class(self, cls, name, virtual_only=False):
        if not cls._meta.abstract:
            _TRIGGERS.append(self)
        super(TriggerVersionField, self).contribute_to_class(cls, name)

    @cached_property
    def trigger_name(self):
        # TODO: this property i due a race contition with mysql backend
        if self._trigger_name is None:
            from concurrency.triggers import get_trigger_name
            self._trigger_name = get_trigger_name(self)
        return self._trigger_name

    def _get_next_version(self, model_instance):
        # always returns the same value
        return int(getattr(model_instance, self.attname, 0))

    def pre_save(self, model_instance, add):
        # always returns the same value
        return int(getattr(model_instance, self.attname, 0))

    @staticmethod
    def _increment_version_number(obj):
        old_value = get_revision_of_object(obj)
        setattr(obj, obj._concurrencymeta._field.attname, int(old_value) + 1)

    @staticmethod
    def _wrap_save(func):
        def inner(self, force_insert=False, force_update=False, using=None, **kwargs):
            reload = kwargs.pop('refetch', False)
            # if self._concurrencymeta.enabled and conf.PROTOCOL == 1:
            #     concurrency_check(self, force_insert, force_update, using, **kwargs)
            ret = func(self, force_insert, force_update, using, **kwargs)
            TriggerVersionField._increment_version_number(self)
            if reload:
                ret = refetch(self)
                setattr(self,
                        self._concurrencymeta._field.attname,
                        get_revision_of_object(ret))

            return ret

        return update_wrapper(inner, func)


try:
    from south.modelsinspector import add_introspection_rules

    rules = [
        (
            (IntegerVersionField, AutoIncVersionField, TriggerVersionField),
            [], {"verbose_name": ["verbose_name", {"default": None}],
                 "name": ["name", {"default": None}],
                 "help_text": ["help_text", {"default": ''}],
                 "db_column": ["db_column", {"default": None}],
                 "db_tablespace": ["db_tablespace", {"default": None}],
                 "default": ["default", {"default": 1}],
                 "manually": ["manually", {"default": False}]})
    ]

    add_introspection_rules(rules, [r"^concurrency\.fields\.IntegerVersionField",
                                    r"^concurrency\.fields\.AutoIncVersionField"])
except ImportError as e:
    from django.conf import settings

    if 'south' in settings.INSTALLED_APPS:
        raise e