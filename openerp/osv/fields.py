# -*- coding: utf-8 -*-
##############################################################################
#
#    OpenERP, Open Source Management Solution
#    Copyright (C) 2004-2009 Tiny SPRL (<http://tiny.be>).
#
#    This program is free software: you can redistribute it and/or modify
#    it under the terms of the GNU Affero General Public License as
#    published by the Free Software Foundation, either version 3 of the
#    License, or (at your option) any later version.
#
#    This program is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU Affero General Public License for more details.
#
#    You should have received a copy of the GNU Affero General Public License
#    along with this program.  If not, see <http://www.gnu.org/licenses/>.
#
##############################################################################

""" Fields:
      - simple
      - relations (one2many, many2one, many2many)
      - function

    Fields Attributes:
        * _classic_read: is a classic sql fields
        * _type   : field type
        * _auto_join: for one2many and many2one fields, tells whether select
            queries will join the relational table instead of replacing the
            field condition by an equivalent-one based on a search.
        * readonly
        * required
        * size
"""

import base64
import datetime as DT
import functools
import logging
import pytz
import re
import xmlrpc.client
from operator import itemgetter
from psycopg2 import Binary

import openerp
import openerp.tools as tools
from openerp.sql_db import LazyCursor
from openerp.tools.translate import _
from openerp.tools import float_repr, float_round, frozendict, html_sanitize
import simplejson
from openerp import SUPERUSER_ID

# deprecated; kept for backward compatibility only
_get_cursor = LazyCursor

EMPTY_DICT = frozendict()

_logger = logging.getLogger(__name__)

def _symbol_set(symb):
    if symb is None or symb == False:
        return None
    elif isinstance(symb, str):
        return symb
    return str(symb)


class _column(object):
    """ Base of all fields, a database column

        An instance of this object is a *description* of a database column. It will
        not hold any data, but only provide the methods to manipulate data of an
        ORM record or even prepare/update the database to hold such a field of data.
    """
    _classic_read = True
    _classic_write = True
    _auto_join = False
    _properties = False
    _type = 'unknown'
    _obj = None
    _multi = False
    _symbol_c = '%s'
    _symbol_f = _symbol_set
    _symbol_set = (_symbol_c, _symbol_f)
    _symbol_get = None
    _deprecated = False

    __slots__ = [
        'copy',                 # whether value is copied by BaseModel.copy()
        'string',
        'help',
        'required',
        'readonly',
        '_domain',
        '_context',
        'states',
        'priority',
        'change_default',
        'size',
        'ondelete',
        'translate',
        'select',
        'manual',
        'write',
        'read',
        'selectable',
        'group_operator',
        'groups',               # CSV list of ext IDs of groups
        'deprecated',           # Optional deprecation warning
        '_args',
        '_prefetch',
    ]

    def __init__(self, string='unknown', required=False, readonly=False, domain=[], context={}, states=None, priority=0, change_default=False, size=None, ondelete=None, translate=False, select=False, manual=False, **args):
        """

        The 'manual' keyword argument specifies if the field is a custom one.
        It corresponds to the 'state' column in ir_model_fields.

        """
        # add parameters and default values
        args['copy'] = args.get('copy', True)
        args['string'] = string
        args['help'] = args.get('help', '')
        args['required'] = required
        args['readonly'] = readonly
        args['_domain'] = domain
        args['_context'] = context
        args['states'] = states
        args['priority'] = priority
        args['change_default'] = change_default
        args['size'] = size
        args['ondelete'] = ondelete.lower() if ondelete else None
        args['translate'] = translate
        args['select'] = select
        args['manual'] = manual
        args['write'] = args.get('write', False)
        args['read'] = args.get('read', False)
        args['selectable'] = args.get('selectable', True)
        args['group_operator'] = args.get('group_operator', None)
        args['groups'] = args.get('groups', None)
        args['deprecated'] = args.get('deprecated', None)
        args['_prefetch'] = args.get('_prefetch', True)

        self._args = EMPTY_DICT
        for key, val in args.items():
            setattr(self, key, val)

        # prefetch only if _classic_write, not deprecated and not manual
        if not self._classic_write or self.deprecated or self.manual:
            self._prefetch = False

    def __getattr__(self, name):
        """ Access a non-slot attribute. """
        if name == '_args':
            raise AttributeError(name)
        try:
            return self._args[name]
        except KeyError:
            raise AttributeError(name)

    def __setattr__(self, name, value):
        """ Set a slot or non-slot attribute. """
        try:
            object.__setattr__(self, name, value)
        except AttributeError:
            if self._args:
                self._args[name] = value
            else:
                self._args = {name: value}     # replace EMPTY_DICT

    def __delattr__(self, name):
        """ Remove a non-slot attribute. """
        try:
            del self._args[name]
        except KeyError:
            raise AttributeError(name)

    def new(self, _computed_field=False, **args):
        """ Return a column like `self` with the given parameters; the parameter
            `_computed_field` tells whether the corresponding field is computed.
        """
        # memory optimization: reuse self whenever possible; you can reduce the
        # average memory usage per registry by 10 megabytes!
        column = type(self)(**args)
        return self if self.to_field_args() == column.to_field_args() else column

    def to_field(self):
        """ convert column `self` to a new-style field """
        from openerp.fields import Field
        return Field.by_type[self._type](column=self, **self.to_field_args())

    def to_field_args(self):
        """ return a dictionary with all the arguments to pass to the field """
        base_items = [
            ('copy', self.copy),
            ('index', self.select),
            ('manual', self.manual),
            ('string', self.string),
            ('help', self.help),
            ('readonly', self.readonly),
            ('required', self.required),
            ('states', self.states),
            ('groups', self.groups),
            ('change_default', self.change_default),
            ('deprecated', self.deprecated),
        ]
        truthy_items = list(filter(itemgetter(1), [
            ('group_operator', self.group_operator),
            ('size', self.size),
            ('ondelete', self.ondelete),
            ('translate', self.translate),
            ('domain', self._domain),
            ('context', self._context),
        ]))
        return dict(base_items + truthy_items + list(self._args.items()))

    def restart(self):
        pass

    def set(self, cr, obj, id, name, value, user=None, context=None):
        cr.execute('update '+obj._table+' set '+name+'='+self._symbol_set[0]+' where id=%s', (self._symbol_set[1](value), id))

    def get(self, cr, obj, ids, name, user=None, offset=0, context=None, values=None):
        raise Exception(_('undefined get method !'))

    def search(self, cr, obj, args, name, value, offset=0, limit=None, uid=None, context=None):
        ids = obj.search(cr, uid, args+self._domain+[(name, 'ilike', value)], offset, limit, context=context)
        res = obj.read(cr, uid, ids, [name], context=context)
        return [x[name] for x in res]

    def as_display_name(self, cr, uid, obj, value, context=None):
        """Converts a field value to a suitable string representation for a record,
           e.g. when this field is used as ``rec_name``.

           :param obj: the ``BaseModel`` instance this column belongs to 
           :param value: a proper value as returned by :py:meth:`~openerp.orm.osv.BaseModel.read`
                         for this column
        """
        # delegated to class method, so a column type A can delegate
        # to a column type B. 
        return self._as_display_name(self, cr, uid, obj, value, context=None)

    @classmethod
    def _as_display_name(cls, field, cr, uid, obj, value, context=None):
        # This needs to be a class method, in case a column type A as to delegate
        # to a column type B.
        return tools.ustr(value)

# ---------------------------------------------------------
# Simple fields
# ---------------------------------------------------------
class boolean(_column):
    _type = 'boolean'
    _symbol_c = '%s'
    _symbol_f = bool
    _symbol_set = (_symbol_c, _symbol_f)
    __slots__ = []

    def __init__(self, string='unknown', required=False, **args):
        super(boolean, self).__init__(string=string, required=required, **args)
        if required:
            _logger.debug(
                "required=True is deprecated: making a boolean field"
                " `required` has no effect, as NULL values are "
                "automatically turned into False. args: %r",args)

class integer(_column):
    _type = 'integer'
    _symbol_c = '%s'
    _symbol_f = lambda x: int(x or 0)
    _symbol_set = (_symbol_c, _symbol_f)
    _symbol_get = lambda self,x: x or 0
    __slots__ = []

    def __init__(self, string='unknown', required=False, **args):
        super(integer, self).__init__(string=string, required=required, **args)

class reference(_column):
    _type = 'reference'
    _classic_read = False # post-process to handle missing target
    __slots__ = ['selection']

    def __init__(self, string, selection, size=None, **args):
        if callable(selection):
            from openerp import api
            selection = api.expected(api.cr_uid_context, selection)
        _column.__init__(self, string=string, size=size, selection=selection, **args)

    def to_field_args(self):
        args = super(reference, self).to_field_args()
        args['selection'] = self.selection
        return args

    def get(self, cr, obj, ids, name, uid=None, context=None, values=None):
        result = {}
        # copy initial values fetched previously.
        for value in values:
            result[value['id']] = value[name]
            if value[name]:
                model, res_id = value[name].split(',')
                if not obj.pool[model].exists(cr, uid, [int(res_id)], context=context):
                    result[value['id']] = False
        return result

    @classmethod
    def _as_display_name(cls, field, cr, uid, obj, value, context=None):
        if value:
            # reference fields have a 'model,id'-like value, that we need to convert
            # to a real name
            model_name, res_id = value.split(',')
            if model_name in obj.pool and res_id:
                model = obj.pool[model_name]
                names = model.name_get(cr, uid, [int(res_id)], context=context)
                return names[0][1] if names else False
        return tools.ustr(value)

# takes a string (encoded in utf8) and returns a string (encoded in utf8)
def _symbol_set_char(self, symb):

    #TODO:
    # * we need to remove the "symb==False" from the next line BUT
    #   for now too many things rely on this broken behavior
    # * the symb==None test should be common to all data types
    if symb is None or symb == False:
        return None

    # we need to convert the string to a unicode object to be able
    # to evaluate its length (and possibly truncate it) reliably
    u_symb = tools.ustr(symb)
    return u_symb[:self.size]

class char(_column):
    _type = 'char'
    __slots__ = ['_symbol_f', '_symbol_set', '_symbol_set_char']

    def __init__(self, string="unknown", size=None, **args):
        _column.__init__(self, string=string, size=size or None, **args)
        # self._symbol_set_char defined to keep the backward compatibility
        self._symbol_f = self._symbol_set_char = lambda x: _symbol_set_char(self, x)
        self._symbol_set = (self._symbol_c, self._symbol_f)

class text(_column):
    _type = 'text'
    __slots__ = []


class html(text):
    _type = 'html'
    _symbol_c = '%s'
    __slots__ = ['_sanitize', '_strip_style', '_symbol_f', '_symbol_set']

    def _symbol_set_html(self, value):
        if value is None or value is False:
            return None
        if not self._sanitize:
            return value
        return html_sanitize(value, strip_style=self._strip_style)

    def __init__(self, string='unknown', sanitize=True, strip_style=False, **args):
        super(html, self).__init__(string=string, **args)
        self._sanitize = sanitize
        self._strip_style = strip_style
        # symbol_set redefinition because of sanitize specific behavior
        self._symbol_f = self._symbol_set_html
        self._symbol_set = (self._symbol_c, self._symbol_f)

    def to_field_args(self):
        args = super(html, self).to_field_args()
        args['sanitize'] = self._sanitize
        args['strip_style'] = self._strip_style
        return args

import builtins

def _symbol_set_float(self, x):
    result = builtins.float(x or 0.0)
    digits = self.digits
    if digits:
        precision, scale = digits
        result = float_repr(float_round(result, precision_digits=scale), precision_digits=scale)
    return result

class float(_column):
    _type = 'float'
    _symbol_c = '%s'
    _symbol_get = lambda self,x: x or 0.0
    __slots__ = ['_digits', '_digits_compute', '_symbol_f', '_symbol_set']

    @property
    def digits(self):
        if self._digits_compute:
            with LazyCursor() as cr:
                return self._digits_compute(cr)
        else:
            return self._digits

    def __init__(self, string='unknown', digits=None, digits_compute=None, required=False, **args):
        _column.__init__(self, string=string, required=required, **args)
        # synopsis: digits_compute(cr) ->  (precision, scale)
        self._digits = digits
        self._digits_compute = digits_compute
        self._symbol_f = lambda x: _symbol_set_float(self, x)
        self._symbol_set = (self._symbol_c, self._symbol_f)

    def to_field_args(self):
        args = super(float, self).to_field_args()
        args['digits'] = self._digits_compute or self._digits
        return args

    def digits_change(self, cr):
        pass

class date(_column):
    _type = 'date'
    __slots__ = []

    MONTHS = [
        ('01', 'January'),
        ('02', 'February'),
        ('03', 'March'),
        ('04', 'April'),
        ('05', 'May'),
        ('06', 'June'),
        ('07', 'July'),
        ('08', 'August'),
        ('09', 'September'),
        ('10', 'October'),
        ('11', 'November'),
        ('12', 'December')
    ]

    @staticmethod
    def today(*args):
        """ Returns the current date in a format fit for being a
        default value to a ``date`` field.

        This method should be provided as is to the _defaults dict, it
        should not be called.
        """
        return DT.date.today().strftime(
            tools.DEFAULT_SERVER_DATE_FORMAT)

    @staticmethod
    def context_today(model, cr, uid, context=None, timestamp=None):
        """Returns the current date as seen in the client's timezone
           in a format fit for date fields.
           This method may be passed as value to initialize _defaults.

           :param Model model: model (osv) for which the date value is being
                               computed - automatically passed when used in
                                _defaults.
           :param datetime timestamp: optional datetime value to use instead of
                                      the current date and time (must be a
                                      datetime, regular dates can't be converted
                                      between timezones.)
           :param dict context: the 'tz' key in the context should give the
                                name of the User/Client timezone (otherwise
                                UTC is used)
           :rtype: str 
        """
        today = timestamp or DT.datetime.now()
        context_today = None
        if context and context.get('tz'):
            tz_name = context['tz']  
        else:
            user = model.pool['res.users'].browse(cr, SUPERUSER_ID, uid)
            tz_name = user.tz
        if tz_name:
            try:
                utc = pytz.timezone('UTC')
                context_tz = pytz.timezone(tz_name)
                utc_today = utc.localize(today, is_dst=False) # UTC = no DST
                context_today = utc_today.astimezone(context_tz)
            except Exception:
                _logger.debug("failed to compute context/client-specific today date, "
                              "using the UTC value for `today`",
                              exc_info=True)
        return (context_today or today).strftime(tools.DEFAULT_SERVER_DATE_FORMAT)

    @staticmethod
    def date_to_datetime(model, cr, uid, userdate, context=None):
        """ Convert date values expressed in user's timezone to
        server-side UTC timestamp, assuming a default arbitrary
        time of 12:00 AM - because a time is needed.

        :param str userdate: date string in in user time zone
        :return: UTC datetime string for server-side use
        """
        user_date = DT.datetime.strptime(userdate, tools.DEFAULT_SERVER_DATE_FORMAT)
        if context and context.get('tz'):
            tz_name = context['tz']
        else:
            tz_name = model.pool.get('res.users').read(cr, SUPERUSER_ID, uid, ['tz'])['tz']
        if tz_name:
            utc = pytz.timezone('UTC')
            context_tz = pytz.timezone(tz_name)
            user_datetime = user_date + DT.timedelta(hours=12.0)
            local_timestamp = context_tz.localize(user_datetime, is_dst=False)
            user_datetime = local_timestamp.astimezone(utc)
            return user_datetime.strftime(tools.DEFAULT_SERVER_DATETIME_FORMAT)
        return user_date.strftime(tools.DEFAULT_SERVER_DATETIME_FORMAT)


class datetime(_column):
    _type = 'datetime'
    __slots__ = []

    MONTHS = [
        ('01', 'January'),
        ('02', 'February'),
        ('03', 'March'),
        ('04', 'April'),
        ('05', 'May'),
        ('06', 'June'),
        ('07', 'July'),
        ('08', 'August'),
        ('09', 'September'),
        ('10', 'October'),
        ('11', 'November'),
        ('12', 'December')
    ]

    @staticmethod
    def now(*args):
        """ Returns the current datetime in a format fit for being a
        default value to a ``datetime`` field.

        This method should be provided as is to the _defaults dict, it
        should not be called.
        """
        return DT.datetime.now().strftime(
            tools.DEFAULT_SERVER_DATETIME_FORMAT)

    @staticmethod
    def context_timestamp(cr, uid, timestamp, context=None):
        """Returns the given timestamp converted to the client's timezone.
           This method is *not* meant for use as a _defaults initializer,
           because datetime fields are automatically converted upon
           display on client side. For _defaults you :meth:`fields.datetime.now`
           should be used instead.

           :param datetime timestamp: naive datetime value (expressed in UTC)
                                      to be converted to the client timezone
           :param dict context: the 'tz' key in the context should give the
                                name of the User/Client timezone (otherwise
                                UTC is used)
           :rtype: datetime
           :return: timestamp converted to timezone-aware datetime in context
                    timezone
        """
        assert isinstance(timestamp, DT.datetime), 'Datetime instance expected'
        if context and context.get('tz'):
            tz_name = context['tz']  
        else:
            registry = openerp.modules.registry.RegistryManager.get(cr.dbname)
            user = registry['res.users'].browse(cr, SUPERUSER_ID, uid)
            tz_name = user.tz
        utc_timestamp = pytz.utc.localize(timestamp, is_dst=False) # UTC = no DST
        if tz_name:
            try:
                context_tz = pytz.timezone(tz_name)
                return utc_timestamp.astimezone(context_tz)
            except Exception:
                _logger.debug("failed to compute context/client-specific timestamp, "
                              "using the UTC value",
                              exc_info=True)
        return utc_timestamp

    @classmethod
    def _as_display_name(cls, field, cr, uid, obj, value, context=None):
        value = datetime.context_timestamp(cr, uid, DT.datetime.strptime(value, tools.DEFAULT_SERVER_DATETIME_FORMAT), context=context)
        return tools.ustr(value.strftime(tools.DEFAULT_SERVER_DATETIME_FORMAT))

class binary(_column):
    _type = 'binary'
    _classic_read = False

    # Binary values may be byte strings (python 2.6 byte array), but
    # the legacy OpenERP convention is to transfer and store binaries
    # as base64-encoded strings. The base64 string may be provided as a
    # unicode in some circumstances, hence the str() cast in symbol_f.
    # This str coercion will only work for pure ASCII unicode strings,
    # on purpose - non base64 data must be passed as a 8bit byte strings.
    _symbol_c = '%s'
    _symbol_f = lambda symb: symb and Binary(str(symb)) or None
    _symbol_set = (_symbol_c, _symbol_f)
    _symbol_get = lambda self, x: x and str(x)

    __slots__ = ['filters']

    def __init__(self, string='unknown', filters=None, **args):
        args['_prefetch'] = args.get('_prefetch', False)
        _column.__init__(self, string=string, filters=filters, **args)

    def get(self, cr, obj, ids, name, user=None, context=None, values=None):
        if not context:
            context = {}
        if not values:
            values = []
        res = {}
        for i in ids:
            val = None
            for v in values:
                if v['id'] == i:
                    val = v[name]
                    break

            # If client is requesting only the size of the field, we return it instead
            # of the content. Presumably a separate request will be done to read the actual
            # content if it's needed at some point.
            # TODO: after 6.0 we should consider returning a dict with size and content instead of
            #       having an implicit convention for the value
            if val and context.get('bin_size_%s' % name, context.get('bin_size')):
                res[i] = tools.human_size(int(val))
            else:
                res[i] = val
        return res

class selection(_column):
    _type = 'selection'
    __slots__ = ['selection']

    def __init__(self, selection, string='unknown', **args):
        if callable(selection):
            from openerp import api
            selection = api.expected(api.cr_uid_context, selection)
        _column.__init__(self, string=string, selection=selection, **args)

    def to_field_args(self):
        args = super(selection, self).to_field_args()
        args['selection'] = self.selection
        return args

    @classmethod
    def reify(cls, cr, uid, model, field, context=None):
        """ Munges the field's ``selection`` attribute as necessary to get
        something useable out of it: calls it if it's a function, applies
        translations to labels if it's not.

        A callable ``selection`` is considered translated on its own.

        :param orm.Model model:
        :param _column field:
        """
        if callable(field.selection):
            return field.selection(model, cr, uid, context)

        if not (context and 'lang' in context):
            return field.selection

        # field_to_dict isn't given a field name, only a field object, we
        # need to get the name back in order to perform the translation lookup
        field_name = next(
            name for name, column in model._columns.items()
            if column == field)

        translation_filter = "%s,%s" % (model._name, field_name)
        translate = functools.partial(
            model.pool['ir.translation']._get_source,
            cr, uid, translation_filter, 'selection', context['lang'])

        return [
            (value, translate(label))
            for value, label in field.selection
        ]

# ---------------------------------------------------------
# Relationals fields
# ---------------------------------------------------------

#
# Values: (0, 0,  { fields })    create
#         (1, ID, { fields })    update
#         (2, ID)                remove (delete)
#         (3, ID)                unlink one (target id or target of relation)
#         (4, ID)                link
#         (5)                    unlink all (only valid for one2many)
#

class many2one(_column):
    _classic_read = False
    _classic_write = True
    _type = 'many2one'
    _symbol_c = '%s'
    _symbol_f = lambda x: x or None
    _symbol_set = (_symbol_c, _symbol_f)

    __slots__ = ['_obj', '_auto_join']

    def __init__(self, obj, string='unknown', auto_join=False, **args):
        args['ondelete'] = args.get('ondelete', 'set null')
        _column.__init__(self, string=string, **args)
        self._obj = obj
        self._auto_join = auto_join

    def to_field_args(self):
        args = super(many2one, self).to_field_args()
        args['comodel_name'] = self._obj
        args['auto_join'] = self._auto_join
        return args

    def set(self, cr, obj_src, id, field, values, user=None, context=None):
        if not context:
            context = {}
        obj = obj_src.pool[self._obj]
        self._table = obj._table
        if type(values) == type([]):
            for act in values:
                if act[0] == 0:
                    id_new = obj.create(cr, act[2])
                    cr.execute('update '+obj_src._table+' set '+field+'=%s where id=%s', (id_new, id))
                elif act[0] == 1:
                    obj.write(cr, [act[1]], act[2], context=context)
                elif act[0] == 2:
                    cr.execute('delete from '+self._table+' where id=%s', (act[1],))
                elif act[0] == 3 or act[0] == 5:
                    cr.execute('update '+obj_src._table+' set '+field+'=null where id=%s', (id,))
                elif act[0] == 4:
                    cr.execute('update '+obj_src._table+' set '+field+'=%s where id=%s', (act[1], id))
        else:
            if values:
                cr.execute('update '+obj_src._table+' set '+field+'=%s where id=%s', (values, id))
            else:
                cr.execute('update '+obj_src._table+' set '+field+'=null where id=%s', (id,))

    def search(self, cr, obj, args, name, value, offset=0, limit=None, uid=None, context=None):
        return obj.pool[self._obj].search(cr, uid, args+self._domain+[('name', 'like', value)], offset, limit, context=context)

    @classmethod
    def _as_display_name(cls, field, cr, uid, obj, value, context=None):
        return value[1] if isinstance(value, tuple) else tools.ustr(value) 


class one2many(_column):
    _classic_read = False
    _classic_write = False
    _type = 'one2many'

    __slots__ = ['_obj', '_fields_id', '_limit', '_auto_join']

    def __init__(self, obj, fields_id, string='unknown', limit=None, auto_join=False, **args):
        # one2many columns are not copied by default
        args['copy'] = args.get('copy', False)
        args['_prefetch'] = args.get('_prefetch', False)
        _column.__init__(self, string=string, **args)
        self._obj = obj
        self._fields_id = fields_id
        self._limit = limit
        self._auto_join = auto_join
        #one2many can't be used as condition for defaults
        assert(self.change_default != True)

    def to_field_args(self):
        args = super(one2many, self).to_field_args()
        args['comodel_name'] = self._obj
        args['inverse_name'] = self._fields_id
        args['auto_join'] = self._auto_join
        args['limit'] = self._limit
        return args

    def get(self, cr, obj, ids, name, user=None, offset=0, context=None, values=None):
        if self._context:
            context = dict(context or {})
            context.update(self._context)

        # retrieve the records in the comodel
        comodel = obj.pool[self._obj].browse(cr, user, [], context)
        inverse = self._fields_id
        domain = self._domain(obj) if callable(self._domain) else self._domain
        domain = domain + [(inverse, 'in', ids)]
        records = comodel.search(domain, limit=self._limit)

        result = {id: [] for id in ids}
        # read the inverse of records without prefetching other fields on them
        for record in records.with_context(prefetch_fields=False):
            # record[inverse] may be a record or an integer
            result[int(record[inverse])].append(record.id)

        return result

    def set(self, cr, obj, id, field, values, user=None, context=None):
        result = []
        context = dict(context or {})
        context.update(self._context)
        if not values:
            return
        original_obj = obj
        obj = obj.pool[self._obj]
        rec = obj.browse(cr, user, [], context=context)
        with rec.env.norecompute():
            _table = obj._table
            for act in values:
                if act[0] == 0:
                    act[2][self._fields_id] = id
                    id_new = obj.create(cr, user, act[2], context=context)
                    result += obj._store_get_values(cr, user, [id_new], list(act[2].keys()), context)
                elif act[0] == 1:
                    obj.write(cr, user, [act[1]], act[2], context=context)
                elif act[0] == 2:
                    obj.unlink(cr, user, [act[1]], context=context)
                elif act[0] == 3:
                    inverse_field = obj._fields.get(self._fields_id)
                    assert inverse_field, 'Trying to unlink the content of a o2m but the pointed model does not have a m2o'
                    # if the model has on delete cascade, just delete the row
                    if inverse_field.ondelete == "cascade":
                        obj.unlink(cr, user, [act[1]], context=context)
                    else:
                        cr.execute('update '+_table+' set '+self._fields_id+'=null where id=%s', (act[1],))
                elif act[0] == 4:
                    # check whether the given record is already linked
                    rec = obj.browse(cr, SUPERUSER_ID, act[1], {'prefetch_fields': False})
                    if int(rec[self._fields_id]) != id:
                        # Must use write() to recompute parent_store structure if needed and check access rules
                        obj.write(cr, user, [act[1]], {self._fields_id:id}, context=context or {})
                elif act[0] == 5:
                    inverse_field = obj._fields.get(self._fields_id)
                    assert inverse_field, 'Trying to unlink the content of a o2m but the pointed model does not have a m2o'
                    # if the o2m has a static domain we must respect it when unlinking
                    domain = (self._domain(original_obj)
                              if callable(self._domain) else self._domain)
                    extra_domain = domain or []
                    ids_to_unlink = obj.search(cr, user, [(self._fields_id,'=',id)] + extra_domain, context=context)
                    # If the model has cascade deletion, we delete the rows because it is the intended behavior,
                    # otherwise we only nullify the reverse foreign key column.
                    if inverse_field.ondelete == "cascade":
                        obj.unlink(cr, user, ids_to_unlink, context=context)
                    else:
                        obj.write(cr, user, ids_to_unlink, {self._fields_id: False}, context=context)
                elif act[0] == 6:
                    # Must use write() to recompute parent_store structure if needed
                    obj.write(cr, user, act[2], {self._fields_id:id}, context=context or {})
                    ids2 = act[2] or [0]
                    cr.execute('select id from '+_table+' where '+self._fields_id+'=%s and id <> ALL (%s)', (id,ids2))
                    ids3 = [x[0] for x in cr.fetchall()]
                    obj.write(cr, user, ids3, {self._fields_id:False}, context=context or {})
        return result

    def search(self, cr, obj, args, name, value, offset=0, limit=None, uid=None, operator='like', context=None):
        domain = self._domain(obj) if callable(self._domain) else self._domain
        return obj.pool[self._obj].name_search(cr, uid, value, domain, operator, context=context,limit=limit)

    @classmethod
    def _as_display_name(cls, field, cr, uid, obj, value, context=None):
        raise NotImplementedError('One2Many columns should not be used as record name (_rec_name)') 

#
# Values: (0, 0,  { fields })    create
#         (1, ID, { fields })    update (write fields to ID)
#         (2, ID)                remove (calls unlink on ID, that will also delete the relationship because of the ondelete)
#         (3, ID)                unlink (delete the relationship between the two objects but does not delete ID)
#         (4, ID)                link (add a relationship)
#         (5, ID)                unlink all
#         (6, ?, ids)            set a list of links
#
class many2many(_column):
    """Encapsulates the logic of a many-to-many bidirectional relationship, handling the
       low-level details of the intermediary relationship table transparently.
       A many-to-many relationship is always symmetrical, and can be declared and accessed
       from either endpoint model.
       If ``rel`` (relationship table name), ``id1`` (source foreign key column name)
       or id2 (destination foreign key column name) are not specified, the system will
       provide default values. This will by default only allow one single symmetrical
       many-to-many relationship between the source and destination model.
       For multiple many-to-many relationship between the same models and for
       relationships where source and destination models are the same, ``rel``, ``id1``
       and ``id2`` should be specified explicitly.

       :param str obj: destination model
       :param str rel: optional name of the intermediary relationship table. If not specified,
                       a canonical name will be derived based on the alphabetically-ordered
                       model names of the source and destination (in the form: ``amodel_bmodel_rel``).
                       Automatic naming is not possible when the source and destination are
                       the same, for obvious ambiguity reasons.
       :param str id1: optional name for the column holding the foreign key to the current
                       model in the relationship table. If not specified, a canonical name
                       will be derived based on the model name (in the form: `src_model_id`).
       :param str id2: optional name for the column holding the foreign key to the destination
                       model in the relationship table. If not specified, a canonical name
                       will be derived based on the model name (in the form: `dest_model_id`)
       :param str string: field label
    """
    _classic_read = False
    _classic_write = False
    _type = 'many2many'

    __slots__ = ['_obj', '_rel', '_id1', '_id2', '_limit', '_auto_join']

    def __init__(self, obj, rel=None, id1=None, id2=None, string='unknown', limit=None, **args):
        """
        """
        args['_prefetch'] = args.get('_prefetch', False)
        _column.__init__(self, string=string, **args)
        self._obj = obj
        if rel and '.' in rel:
            raise Exception(_('The second argument of the many2many field %s must be a SQL table !'\
                'You used %s, which is not a valid SQL table name.')% (string,rel))
        self._rel = rel
        self._id1 = id1
        self._id2 = id2
        self._limit = limit
        self._auto_join = False

    def to_field_args(self):
        args = super(many2many, self).to_field_args()
        args['comodel_name'] = self._obj
        args['relation'] = self._rel
        args['column1'] = self._id1
        args['column2'] = self._id2
        args['limit'] = self._limit
        return args

    def _sql_names(self, source_model):
        """Return the SQL names defining the structure of the m2m relationship table

            :return: (m2m_table, local_col, dest_col) where m2m_table is the table name,
                     local_col is the name of the column holding the current model's FK, and
                     dest_col is the name of the column holding the destination model's FK, and
        """
        tbl, col1, col2 = self._rel, self._id1, self._id2
        if not all((tbl, col1, col2)):
            # the default table name is based on the stable alphabetical order of tables
            dest_model = source_model.pool[self._obj]
            tables = tuple(sorted([source_model._table, dest_model._table]))
            if not tbl:
                assert tables[0] != tables[1], 'Implicit/Canonical naming of m2m relationship table '\
                                               'is not possible when source and destination models are '\
                                               'the same'
                tbl = '%s_%s_rel' % tables
            if not col1:
                col1 = '%s_id' % source_model._table
            if not col2:
                col2 = '%s_id' % dest_model._table
        return tbl, col1, col2

    def _get_query_and_where_params(self, cr, model, ids, values, where_params):
        """ Extracted from ``get`` to facilitate fine-tuning of the generated
            query. """
        query = 'SELECT %(rel)s.%(id2)s, %(rel)s.%(id1)s \
                   FROM %(rel)s, %(from_c)s \
                  WHERE %(rel)s.%(id1)s IN %%s \
                    AND %(rel)s.%(id2)s = %(tbl)s.id \
                 %(where_c)s  \
                 %(order_by)s \
                 %(limit)s \
                 OFFSET %(offset)d' \
                 % values
        return query, where_params

    def get(self, cr, model, ids, name, user=None, offset=0, context=None, values=None):
        if not context:
            context = {}
        if not values:
            values = {}
        res = {}
        if not ids:
            return res
        for id in ids:
            res[id] = []
        if offset:
            _logger.warning(
                "Specifying offset at a many2many.get() is deprecated and may"
                " produce unpredictable results.")
        obj = model.pool[self._obj]
        rel, id1, id2 = self._sql_names(model)

        # static domains are lists, and are evaluated both here and on client-side, while string
        # domains supposed by dynamic and evaluated on client-side only (thus ignored here)
        # FIXME: make this distinction explicit in API!
        domain = isinstance(self._domain, list) and self._domain or []

        wquery = obj._where_calc(cr, user, domain, context=context)
        obj._apply_ir_rules(cr, user, wquery, 'read', context=context)
        order_by = obj._generate_order_by(None, wquery)
        from_c, where_c, where_params = wquery.get_sql()
        if where_c:
            where_c = ' AND ' + where_c

        limit_str = ''
        if self._limit is not None:
            limit_str = ' LIMIT %d' % self._limit

        query, where_params = self._get_query_and_where_params(cr, model, ids, {'rel': rel,
               'from_c': from_c,
               'tbl': obj._table,
               'id1': id1,
               'id2': id2,
               'where_c': where_c,
               'limit': limit_str,
               'order_by': order_by,
               'offset': offset,
                }, where_params)

        cr.execute(query, [tuple(ids),] + where_params)
        for r in cr.fetchall():
            res[r[1]].append(r[0])
        return res

    def set(self, cr, model, id, name, values, user=None, context=None):
        if not context:
            context = {}
        if not values:
            return
        rel, id1, id2 = self._sql_names(model)
        obj = model.pool[self._obj]

        def link(ids):
            # beware of duplicates when inserting
            query = """ INSERT INTO {rel} ({id1}, {id2})
                        (SELECT %s, unnest(%s)) EXCEPT (SELECT {id1}, {id2} FROM {rel} WHERE {id1}=%s)
                    """.format(rel=rel, id1=id1, id2=id2)
            for sub_ids in cr.split_for_in_conditions(ids):
                cr.execute(query, (id, list(sub_ids), id))

        def unlink_all():
            # remove all records for which user has access rights
            clauses, params, tables = obj.pool.get('ir.rule').domain_get(cr, user, obj._name, context=context)
            cond = " AND ".join(clauses) if clauses else "1=1"
            query = """ DELETE FROM {rel} USING {tables}
                        WHERE {rel}.{id1}=%s AND {rel}.{id2}={table}.id AND {cond}
                    """.format(rel=rel, id1=id1, id2=id2,
                               table=obj._table, tables=','.join(tables), cond=cond)
            cr.execute(query, [id] + params)

        for act in values:
            if not (isinstance(act, list) or isinstance(act, tuple)) or not act:
                continue
            if act[0] == 0:
                idnew = obj.create(cr, user, act[2], context=context)
                cr.execute('insert into '+rel+' ('+id1+','+id2+') values (%s,%s)', (id, idnew))
            elif act[0] == 1:
                obj.write(cr, user, [act[1]], act[2], context=context)
            elif act[0] == 2:
                obj.unlink(cr, user, [act[1]], context=context)
            elif act[0] == 3:
                cr.execute('delete from '+rel+' where ' + id1 + '=%s and '+ id2 + '=%s', (id, act[1]))
            elif act[0] == 4:
                link([act[1]])
            elif act[0] == 5:
                unlink_all()
            elif act[0] == 6:
                unlink_all()
                link(act[2])

    #
    # TODO: use a name_search
    #
    def search(self, cr, obj, args, name, value, offset=0, limit=None, uid=None, operator='like', context=None):
        return obj.pool[self._obj].search(cr, uid, args+self._domain+[('name', operator, value)], offset, limit, context=context)

    @classmethod
    def _as_display_name(cls, field, cr, uid, obj, value, context=None):
        raise NotImplementedError('Many2Many columns should not be used as record name (_rec_name)') 


def get_nice_size(value):
    size = 0
    if isinstance(value, int):
        size = value
    elif value: # this is supposed to be a string
        size = len(value)
        if size < 12:  # suppose human size
            return value
    return tools.human_size(size)

# See http://www.w3.org/TR/2000/REC-xml-20001006#NT-Char
# and http://bugs.python.org/issue10066
invalid_xml_low_bytes = re.compile(r'[\x00-\x08\x0b-\x0c\x0e-\x1f]')

def sanitize_binary_value(value):
    # binary fields should be 7-bit ASCII base64-encoded data,
    # but we do additional sanity checks to make sure the values
    # are not something else that won't pass via XML-RPC
    if isinstance(value, (xmlrpc.client.Binary, tuple, list, dict)):
        # these builtin types are meant to pass untouched
        return value

    # Handle invalid bytes values that will cause problems
    # for XML-RPC. See for more info:
    #  - http://bugs.python.org/issue10066
    #  - http://www.w3.org/TR/2000/REC-xml-20001006#NT-Char

    # Coercing to unicode would normally allow it to properly pass via
    # XML-RPC, transparently encoded as UTF-8 by xmlrpclib.
    # (this works for _any_ byte values, thanks to the fallback
    #  to latin-1 passthrough encoding when decoding to unicode)
    value = tools.ustr(value)

    # Due to Python bug #10066 this could still yield invalid XML
    # bytes, specifically in the low byte range, that will crash
    # the decoding side: [\x00-\x08\x0b-\x0c\x0e-\x1f]
    # So check for low bytes values, and if any, perform
    # base64 encoding - not very smart or useful, but this is
    # our last resort to avoid crashing the request.
    if invalid_xml_low_bytes.search(value):
        # b64-encode after restoring the pure bytes with latin-1
        # passthrough encoding
        value = base64.b64encode(value.encode('latin-1'))

    return value


# ---------------------------------------------------------
# Function fields
# ---------------------------------------------------------
class function(_column):
    """
    A field whose value is computed by a function (rather
    than being read from the database).

    :param fnct: the callable that will compute the field value.
    :param arg: arbitrary value to be passed to ``fnct`` when computing the value.
    :param fnct_inv: the callable that will allow writing values in that field
                     (if not provided, the field is read-only).
    :param fnct_inv_arg: arbitrary value to be passed to ``fnct_inv`` when
                         writing a value.
    :param str type: type of the field simulated by the function field
    :param fnct_search: the callable that allows searching on the field
                        (if not provided, search will not return any result).
    :param store: store computed value in database
                  (see :ref:`The *store* parameter <field-function-store>`).
    :type store: True or dict specifying triggers for field computation
    :param multi: name of batch for batch computation of function fields.
                  All fields with the same batch name will be computed by
                  a single function call. This changes the signature of the
                  ``fnct`` callable.

    .. _field-function-fnct: The ``fnct`` parameter

    .. rubric:: The ``fnct`` parameter

    The callable implementing the function field must have the following signature:

    .. function:: fnct(model, cr, uid, ids, field_name(s), arg, context)

        Implements the function field.

        :param orm model: model to which the field belongs (should be ``self`` for
                          a model method)
        :param field_name(s): name of the field to compute, or if ``multi`` is provided,
                              list of field names to compute.
        :type field_name(s): str | [str]
        :param arg: arbitrary value passed when declaring the function field
        :rtype: dict
        :return: mapping of ``ids`` to computed values, or if multi is provided,
                 to a map of field_names to computed values

    The values in the returned dictionary must be of the type specified by the type
    argument in the field declaration.

    Here is an example with a simple function ``char`` function field::

        # declarations
        def compute(self, cr, uid, ids, field_name, arg, context):
            result = {}
            # ...
            return result
        _columns['my_char'] = fields.function(compute, type='char', size=50)

        # when called with ``ids=[1,2,3]``, ``compute`` could return:
        {
            1: 'foo',
            2: 'bar',
            3: False # null values should be returned explicitly too
        }

    If ``multi`` is set, then ``field_name`` is replaced by ``field_names``: a list
    of the field names that should be computed. Each value in the returned
    dictionary must then be a dictionary mapping field names to values.

    Here is an example where two function fields (``name`` and ``age``)
    are both computed by a single function field::

        # declarations
        def compute(self, cr, uid, ids, field_names, arg, context):
            result = {}
            # ...
            return result
        _columns['name'] = fields.function(compute_person_data, type='char',\
                                           size=50, multi='person_data')
        _columns[''age'] = fields.function(compute_person_data, type='integer',\
                                           multi='person_data')

        # when called with ``ids=[1,2,3]``, ``compute_person_data`` could return:
        {
            1: {'name': 'Bob', 'age': 23},
            2: {'name': 'Sally', 'age': 19},
            3: {'name': 'unknown', 'age': False}
        }

    .. _field-function-fnct-inv:

    .. rubric:: The ``fnct_inv`` parameter

    This callable implements the write operation for the function field
    and must have the following signature:

    .. function:: fnct_inv(model, cr, uid, id, field_name, field_value, fnct_inv_arg, context)

        Callable that implements the ``write`` operation for the function field.

        :param orm model: model to which the field belongs (should be ``self`` for
                          a model method)
        :param int id: the identifier of the object to write on
        :param str field_name: name of the field to set
        :param fnct_inv_arg: arbitrary value passed when declaring the function field
        :return: True

    When writing values for a function field, the ``multi`` parameter is ignored.

    .. _field-function-fnct-search:

    .. rubric:: The ``fnct_search`` parameter

    This callable implements the search operation for the function field
    and must have the following signature:

    .. function:: fnct_search(model, cr, uid, model_again, field_name, criterion, context)

        Callable that implements the ``search`` operation for the function field by expanding
        a search criterion based on the function field into a new domain based only on
        columns that are stored in the database.

        :param orm model: model to which the field belongs (should be ``self`` for
                          a model method)
        :param orm model_again: same value as ``model`` (seriously! this is for backwards
                                compatibility)
        :param str field_name: name of the field to search on
        :param list criterion: domain component specifying the search criterion on the field.
        :rtype: list
        :return: domain to use instead of ``criterion`` when performing the search.
                 This new domain must be based only on columns stored in the database, as it
                 will be used directly without any translation.

        The returned value must be a domain, that is, a list of the form [(field_name, operator, operand)].
        The most generic way to implement ``fnct_search`` is to directly search for the records that
        match the given ``criterion``, and return their ``ids`` wrapped in a domain, such as
        ``[('id','in',[1,3,5])]``.

    .. _field-function-store:

    .. rubric:: The ``store`` parameter

    The ``store`` parameter allows caching the result of the field computation in the
    database, and defining the triggers that will invalidate that cache and force a
    recomputation of the function field.
    When not provided, the field is computed every time its value is read.
    The value of ``store`` may be either ``True`` (to recompute the field value whenever
    any field in the same record is modified), or a dictionary specifying a more
    flexible set of recomputation triggers.

    A trigger specification is a dictionary that maps the names of the models that
    will trigger the computation, to a tuple describing the trigger rule, in the
    following form::

        store = {
            'trigger_model': (mapping_function,
                              ['trigger_field1', 'trigger_field2'],
                              priority),
        }

    A trigger rule is defined by a 3-item tuple where:

        * The ``mapping_function`` is defined as follows:

            .. function:: mapping_function(trigger_model, cr, uid, trigger_ids, context)

                Callable that maps record ids of a trigger model to ids of the
                corresponding records in the source model (whose field values
                need to be recomputed).

                :param orm model: trigger_model
                :param list trigger_ids: ids of the records of trigger_model that were
                                         modified
                :rtype: list
                :return: list of ids of the source model whose function field values
                         need to be recomputed

        * The second item is a list of the fields who should act as triggers for
          the computation. If an empty list is given, all fields will act as triggers.
        * The last item is the priority, used to order the triggers when processing them
          after any write operation on a model that has function field triggers. The
          default priority is 10.

    In fact, setting store = True is the same as using the following trigger dict::

        store = {
              'model_itself': (lambda self, cr, uid, ids, context: ids,
                               [],
                               10)
        }

    """
    _properties = True

    __slots__ = [
        '_type',
        '_classic_read',
        '_classic_write',
        '_symbol_c',
        '_symbol_f',
        '_symbol_set',
        '_symbol_get',

        '_fnct',
        '_arg',
        '_fnct_inv',
        '_fnct_inv_arg',
        '_fnct_search',
        '_multi',
        'store',

        '_digits',
        '_digits_compute',
        'selection',
        '_obj',
    ]

    @property
    def digits(self):
        if self._digits_compute:
            with LazyCursor() as cr:
                return self._digits_compute(cr)
        else:
            return self._digits

#
# multi: compute several fields in one call
#
    def __init__(self, fnct, arg=None, fnct_inv=None, fnct_inv_arg=None, type='float', fnct_search=None, obj=None, store=False, multi=False, **args):
        self._classic_read = False
        self._classic_write = False
        self._prefetch = False
        self._symbol_c = '%s'
        self._symbol_f = _symbol_set
        self._symbol_set = (self._symbol_c, self._symbol_f)
        self._symbol_get = None

        # pop attributes that should not be assigned to self
        self._digits = args.pop('digits', (16,2))
        self._digits_compute = args.pop('digits_compute', None)
        self._obj = args.pop('relation', obj)

        # function fields are not copied by default
        args['copy'] = args.get('copy', False)

        _column.__init__(self, **args)

        self._type = type
        self._fnct = fnct
        self._arg = arg
        self._fnct_inv = fnct_inv
        self._fnct_inv_arg = fnct_inv_arg
        self._fnct_search = fnct_search
        self.store = store
        self._multi = multi

        if not fnct_inv:
            self.readonly = 1

        if not fnct_search and not store:
            self.selectable = False

        if callable(args.get('selection')):
            from openerp import api
            self.selection = api.expected(api.cr_uid_context, args['selection'])

        if store:
            if self._type != 'many2one':
                # m2o fields need to return tuples with name_get, not just foreign keys
                self._classic_read = True
            self._classic_write = True
            if type=='binary':
                self._symbol_get=lambda x:x and str(x)
            else:
                self._prefetch = True

        if type == 'char':
            self._symbol_c = char._symbol_c
            self._symbol_f = lambda x: _symbol_set_char(self, x)
            self._symbol_set = (self._symbol_c, self._symbol_f)
        elif type == 'float':
            self._symbol_c = float._symbol_c
            self._symbol_f = lambda x: _symbol_set_float(self, x)
            self._symbol_set = (self._symbol_c, self._symbol_f)
        else:
            type_class = globals().get(type)
            if type_class is not None:
                self._symbol_c = type_class._symbol_c
                self._symbol_f = type_class._symbol_f
                self._symbol_set = type_class._symbol_set

    def new(self, _computed_field=False, **args):
        if _computed_field:
            # field is computed, we need an instance of a non-function column
            type_class = globals()[self._type]
            return type_class(**args)
        else:
            # HACK: function fields are tricky to recreate, simply return a copy
            import copy
            return copy.copy(self)

    def to_field_args(self):
        args = super(function, self).to_field_args()
        args['store'] = bool(self.store)
        args['company_dependent'] = False
        if self._type in ('float',):
            args['digits'] = self._digits_compute or self._digits
        elif self._type in ('selection', 'reference'):
            args['selection'] = self.selection
        elif self._type in ('many2one', 'one2many', 'many2many'):
            args['comodel_name'] = self._obj
        return args

    def digits_change(self, cr):
        pass

    def search(self, cr, uid, obj, name, args, context=None):
        if not self._fnct_search:
            #CHECKME: should raise an exception
            return []
        return self._fnct_search(obj, cr, uid, obj, name, args, context=context)

    def postprocess(self, cr, uid, obj, field, value=None, context=None):
        return self._postprocess_batch(cr, uid, obj, field, {0: value}, context=context)[0]

    def _postprocess_batch(self, cr, uid, obj, field, values, context=None):
        if not values:
            return values

        if context is None:
            context = {}

        field_type = obj._columns[field]._type
        new_values = dict(values)

        if field_type == 'binary':
            if context.get('bin_size'):
                # client requests only the size of binary fields
                for rid, value in values.items():
                    if value:
                        new_values[rid] = get_nice_size(value)
            elif not context.get('bin_raw'):
                for rid, value in values.items():
                    if value:
                        new_values[rid] = sanitize_binary_value(value)

        return new_values

    def get(self, cr, obj, ids, name, uid=False, context=None, values=None):
        multi = self._multi
        # if we already have a value, don't recompute it.
        # This happen if case of stored many2one fields
        if values and not multi and name in values[0]:
            result = dict((v['id'], v[name]) for v in values)
        elif values and multi and all(n in values[0] for n in name):
            result = dict((v['id'], dict((n, v[n]) for n in name)) for v in values)
        else:
            result = self._fnct(obj, cr, uid, ids, name, self._arg, context)
        if multi:
            swap = {}
            for rid, values in result.items():
                for f, v in values.items():
                    if f not in name:
                        continue
                    swap.setdefault(f, {})[rid] = v

            for field, values in swap.items():
                new_values = self._postprocess_batch(cr, uid, obj, field, values, context)
                for rid, value in new_values.items():
                    result[rid][field] = value

        else:
            result = self._postprocess_batch(cr, uid, obj, name, result, context)

        return result

    def set(self, cr, obj, id, name, value, user=None, context=None):
        if not context:
            context = {}
        if self._fnct_inv:
            self._fnct_inv(obj, cr, user, id, name, value, self._fnct_inv_arg, context)

    @classmethod
    def _as_display_name(cls, field, cr, uid, obj, value, context=None):
        # Function fields are supposed to emulate a basic field type,
        # so they can delegate to the basic type for record name rendering
        return globals()[field._type]._as_display_name(field, cr, uid, obj, value, context=context)

# ---------------------------------------------------------
# Related fields
# ---------------------------------------------------------

class related(function):
    """Field that points to some data inside another field of the current record.

    Example::

       _columns = {
           'foo_id': fields.many2one('my.foo', 'Foo'),
           'bar': fields.related('foo_id', 'frol', type='char', string='Frol of Foo'),
        }
    """
    __slots__ = ['arg', '_relations']

    def _related_search(self, tobj, cr, uid, obj=None, name=None, domain=None, context=None):
        # assume self._arg = ('foo', 'bar', 'baz')
        # domain = [(name, op, val)]   =>   search [('foo.bar.baz', op, val)]
        field = '.'.join(self._arg)
        return [(field, x[1], x[2]) for x in domain]

    def _related_write(self, obj, cr, uid, ids, field_name, values, args, context=None):
        if isinstance(ids, int):
            ids = [ids]
        for instance in obj.browse(cr, uid, ids, context=context):
            # traverse all fields except the last one
            for field in self.arg[:-1]:
                instance = instance[field][:1]
            if instance:
                # write on the last field of the target record
                instance.write({self.arg[-1]: values})

    def _related_read(self, obj, cr, uid, ids, field_name, args, context=None):
        res = {}
        for record in obj.browse(cr, SUPERUSER_ID, ids, context=context):
            value = record
            # traverse all fields except the last one
            for field in self.arg[:-1]:
                value = value[field][:1]
            # read the last field on the target record
            res[record.id] = value[self.arg[-1]]

        if self._type == 'many2one':
            # res[id] is a recordset; convert it to (id, name) or False.
            # Perform name_get as root, as seeing the name of a related object depends on
            # access right of source document, not target, so user may not have access.
            value_ids = list(set(value.id for value in res.values() if value))
            value_name = dict(obj.pool[self._obj].name_get(cr, SUPERUSER_ID, value_ids, context=context))
            res = dict((id, bool(value) and (value.id, value_name[value.id])) for id, value in res.items())

        elif self._type in ('one2many', 'many2many'):
            # res[id] is a recordset; convert it to a list of ids
            res = dict((id, value.ids) for id, value in res.items())

        return res

    def __init__(self, *arg, **args):
        self.arg = arg
        self._relations = []
        super(related, self).__init__(self._related_read, arg, self._related_write, fnct_inv_arg=arg, fnct_search=self._related_search, **args)
        if self.store is True:
            # TODO: improve here to change self.store = {...} according to related objects
            pass


class sparse(function):
    __slots__ = ['serialization_field']

    def convert_value(self, obj, cr, uid, record, value, read_value, context=None):        
        """
            + For a many2many field, a list of tuples is expected.
              Here is the list of tuple that are accepted, with the corresponding semantics ::

                 (0, 0,  { values })    link to a new record that needs to be created with the given values dictionary
                 (1, ID, { values })    update the linked record with id = ID (write *values* on it)
                 (2, ID)                remove and delete the linked record with id = ID (calls unlink on ID, that will delete the object completely, and the link to it as well)
                 (3, ID)                cut the link to the linked record with id = ID (delete the relationship between the two objects but does not delete the target object itself)
                 (4, ID)                link to existing record with id = ID (adds a relationship)
                 (5)                    unlink all (like using (3,ID) for all linked records)
                 (6, 0, [IDs])          replace the list of linked IDs (like using (5) then (4,ID) for each ID in the list of IDs)

                 Example:
                    [(6, 0, [8, 5, 6, 4])] sets the many2many to ids [8, 5, 6, 4]

            + For a one2many field, a lits of tuples is expected.
              Here is the list of tuple that are accepted, with the corresponding semantics ::

                 (0, 0,  { values })    link to a new record that needs to be created with the given values dictionary
                 (1, ID, { values })    update the linked record with id = ID (write *values* on it)
                 (2, ID)                remove and delete the linked record with id = ID (calls unlink on ID, that will delete the object completely, and the link to it as well)

                 Example:
                    [(0, 0, {'field_name':field_value_record1, ...}), (0, 0, {'field_name':field_value_record2, ...})]
        """

        if self._type == 'many2many':
            if not value:
                return []
            assert value[0][0] == 6, 'Unsupported m2m value for sparse field: %s' % value
            return value[0][2]

        elif self._type == 'one2many':
            if not read_value:
                read_value = []
            relation_obj = obj.pool[self.relation]
            for vals in value:
                assert vals[0] in (0,1,2), 'Unsupported o2m value for sparse field: %s' % vals
                if vals[0] == 0:
                    read_value.append(relation_obj.create(cr, uid, vals[2], context=context))
                elif vals[0] == 1:
                    relation_obj.write(cr, uid, vals[1], vals[2], context=context)
                elif vals[0] == 2:
                    relation_obj.unlink(cr, uid, vals[1], context=context)
                    read_value.remove(vals[1])
            return read_value
        return value

    def _sparse_write(self,obj,cr, uid, ids, field_name, value, args, context=None):
        if not type(ids) == list:
            ids = [ids]
        records = obj.browse(cr, uid, ids, context=context)
        for record in records:
            # grab serialized value as object - already deserialized
            serialized = getattr(record, self.serialization_field)
            if value is None:
                # simply delete the key to unset it.
                serialized.pop(field_name, None)
            else: 
                serialized[field_name] = self.convert_value(obj, cr, uid, record, value, serialized.get(field_name), context=context)
            obj.write(cr, uid, ids, {self.serialization_field: serialized}, context=context)
        return True

    def _sparse_read(self, obj, cr, uid, ids, field_names, args, context=None):
        results = {}
        records = obj.browse(cr, uid, ids, context=context)
        for record in records:
            # grab serialized value as object - already deserialized
            serialized = getattr(record, self.serialization_field)
            results[record.id] = {}
            for field_name in field_names:
                field_type = obj._columns[field_name]._type
                value = serialized.get(field_name, False)
                if field_type in ('one2many','many2many'):
                    value = value or []
                    if value:
                        # filter out deleted records as superuser
                        relation_obj = obj.pool[obj._columns[field_name].relation]
                        value = relation_obj.exists(cr, openerp.SUPERUSER_ID, value)
                if type(value) in (int,int) and field_type == 'many2one':
                    relation_obj = obj.pool[obj._columns[field_name].relation]
                    # check for deleted record as superuser
                    if not relation_obj.exists(cr, openerp.SUPERUSER_ID, [value]):
                        value = False
                results[record.id][field_name] = value
        return results

    def __init__(self, serialization_field, **kwargs):
        self.serialization_field = serialization_field
        super(sparse, self).__init__(self._sparse_read, fnct_inv=self._sparse_write, multi='__sparse_multi', **kwargs)


# ---------------------------------------------------------
# Dummy fields
# ---------------------------------------------------------

class dummy(function):
    __slots__ = ['arg', '_relations']

    def _dummy_search(self, tobj, cr, uid, obj=None, name=None, domain=None, context=None):
        return []

    def _dummy_write(self, obj, cr, uid, ids, field_name, values, args, context=None):
        return False

    def _dummy_read(self, obj, cr, uid, ids, field_name, args, context=None):
        return {}

    def __init__(self, *arg, **args):
        self.arg = arg
        self._relations = []
        super(dummy, self).__init__(self._dummy_read, arg, self._dummy_write, fnct_inv_arg=arg, fnct_search=self._dummy_search, **args)

# ---------------------------------------------------------
# Serialized fields
# ---------------------------------------------------------

class serialized(_column):
    """ A field able to store an arbitrary python data structure.
    
        Note: only plain components allowed.
    """
    _type = 'serialized'
    __slots__ = []

    def _symbol_set_struct(val):
        return simplejson.dumps(val)

    def _symbol_get_struct(self, val):
        return simplejson.loads(val or '{}')

    _symbol_c = '%s'
    _symbol_f = _symbol_set_struct
    _symbol_set = (_symbol_c, _symbol_f)
    _symbol_get = _symbol_get_struct

    def __init__(self, *args, **kwargs):
        kwargs['_prefetch'] = kwargs.get('_prefetch', False)
        super(serialized, self).__init__(*args, **kwargs)

# TODO: review completly this class for speed improvement
class property(function):
    __slots__ = []

    def to_field_args(self):
        args = super(property, self).to_field_args()
        args['company_dependent'] = True
        return args

    def _property_search(self, tobj, cr, uid, obj, name, domain, context=None):
        ir_property = obj.pool['ir.property']
        result = []
        for field, operator, value in domain:
            result += ir_property.search_multi(cr, uid, name, tobj._name, operator, value, context=context)
        return result

    def _property_write(self, obj, cr, uid, id, prop_name, value, obj_dest, context=None):
        ir_property = obj.pool['ir.property']
        ir_property.set_multi(cr, uid, prop_name, obj._name, {id: value}, context=context)
        return True

    def _property_read(self, obj, cr, uid, ids, prop_names, obj_dest, context=None):
        ir_property = obj.pool['ir.property']

        res = {id: {} for id in ids}
        for prop_name in prop_names:
            field = obj._fields[prop_name]
            values = ir_property.get_multi(cr, uid, prop_name, obj._name, ids, context=context)
            if field.type == 'many2one':
                # name_get the non-null values as SUPERUSER_ID
                vals = sum(set([_f for _f in iter(values.values()) if _f]),
                           obj.pool[field.comodel_name].browse(cr, uid, [], context=context))
                vals_name = dict(vals.sudo().name_get()) if vals else {}
                for id, value in values.items():
                    ng = False
                    if value and value.id in vals_name:
                        ng = value.id, vals_name[value.id]
                    res[id][prop_name] = ng
            else:
                for id, value in values.items():
                    res[id][prop_name] = value

        return res

    def __init__(self, **args):
        if 'view_load' in args:
            _logger.warning("view_load attribute is deprecated on ir.fields. Args: %r", args)
        args = dict(args)
        args['obj'] = args.pop('relation', '') or args.get('obj', '')
        super(property, self).__init__(
            fnct=self._property_read,
            fnct_inv=self._property_write,
            fnct_search=self._property_search,
            multi='properties',
            **args
        )


class column_info(object):
    """ Struct containing details about an osv column, either one local to
        its model, or one inherited via _inherits.

        .. attribute:: name

            name of the column

        .. attribute:: column

            column instance, subclass of :class:`_column`

        .. attribute:: parent_model

            if the column is inherited, name of the model that contains it,
            ``None`` for local columns.

        .. attribute:: parent_column

            the name of the column containing the m2o relationship to the
            parent model that contains this column, ``None`` for local columns.

        .. attribute:: original_parent

            if the column is inherited, name of the original parent model that
            contains it i.e in case of multilevel inheritance, ``None`` for
            local columns.
    """
    __slots__ = ['name', 'column', 'parent_model', 'parent_column', 'original_parent']

    def __init__(self, name, column, parent_model=None, parent_column=None, original_parent=None):
        self.name = name
        self.column = column
        self.parent_model = parent_model
        self.parent_column = parent_column
        self.original_parent = original_parent

    def __str__(self):
        return '%s(%s, %s, %s, %s, %s)' % (
            self.__class__.__name__, self.name, self.column,
            self.parent_model, self.parent_column, self.original_parent)


# vim:expandtab:smartindent:tabstop=4:softtabstop=4:shiftwidth=4:
