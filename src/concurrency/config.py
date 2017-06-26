# coding=utf-8
from __future__ import absolute_import, unicode_literals

from django.core.exceptions import ImproperlyConfigured
from django.test.signals import setting_changed
from django.utils import six

from .compat import get_callable

# List Editable Policy
# 0 do not save updated records, save others, show message to the user
# 1 abort whole transaction

CONCURRENCY_LIST_EDITABLE_POLICY_SILENT = 1
CONCURRENCY_LIST_EDITABLE_POLICY_ABORT_ALL = 2
CONCURRENCY_POLICY_RAISE = 4
CONCURRENCY_POLICY_CALLBACK = 8

LIST_EDITABLE_POLICIES = [CONCURRENCY_LIST_EDITABLE_POLICY_SILENT, CONCURRENCY_LIST_EDITABLE_POLICY_ABORT_ALL]


class AppSettings(object):
    """
    Class to manage application related settings
    How to use:
    >>> import pytest
    >>> from django.conf import settings
    >>> from concurrency.utils import fqn
    >>> settings.APP_OVERRIDE = 'overridden'
    >>> class MySettings(AppSettings):
    ...     defaults = {'ENTRY1': 'abc', 'ENTRY2': 123, 'OVERRIDE': None, 'CALLBACK': fqn(fqn)}

    >>> conf = MySettings("APP")
    >>> str(conf.ENTRY1), str(settings.APP_ENTRY1)
    ('abc', 'abc')
    >>> str(conf.OVERRIDE), str(settings.APP_OVERRIDE)
    ('overridden', 'overridden')

    >>> conf = MySettings("MYAPP")
    >>> conf.ENTRY2, settings.MYAPP_ENTRY2
    (123, 123)
    >>> settings.MYAPP_CALLBACK = fqn
    >>> conf = MySettings("MYAPP")
    >>> conf.CALLBACK == fqn
    True
    >>> with pytest.raises(ImproperlyConfigured):
    ...     settings.OTHER_CALLBACK = 222
    ...     conf = MySettings("OTHER")

    """
    defaults = {
        'ENABLED': True,
        'MANUAL_TRIGGERS': False,
        'FIELD_SIGNER': 'concurrency.forms.VersionFieldSigner',
        'POLICY': CONCURRENCY_LIST_EDITABLE_POLICY_SILENT,
        'CALLBACK': 'concurrency.views.callback',
        'HANDLER409': 'concurrency.views.conflict',
        'IGNORE_DEFAULT': True,
    }

    def __init__(self, prefix):
        """
        Loads our settings from django.conf.settings, applying defaults for any
        that are omitted.
        """
        self.prefix = prefix
        from django.conf import settings

        for name, default in self.defaults.items():
            prefix_name = (self.prefix + '_' + name).upper()
            value = getattr(settings, prefix_name, default)
            self._set_attr(prefix_name, value)
            setattr(settings, prefix_name, value)
            setting_changed.send(self.__class__, setting=prefix_name, value=value, enter=True)

        setting_changed.connect(self._handler)

    def _set_attr(self, prefix_name, value):
        name = prefix_name[len(self.prefix) + 1:]
        if name == 'CALLBACK':
            if isinstance(value, six.string_types):
                func = get_callable(value)
            elif callable(value):
                func = value
            else:
                raise ImproperlyConfigured("{} is not a valid value for `CALLBACK`. It must be a callable or a fullpath to callable. ".format(value))
            self._callback = func

        setattr(self, name, value)

    def _handler(self, sender, setting, value, **kwargs):
        """
            handler for ``setting_changed`` signal.

        @see :ref:`django:setting-changed`_
        """
        if setting.startswith(self.prefix):
            self._set_attr(setting, value)


conf = AppSettings('CONCURRENCY')
