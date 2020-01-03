# -*- coding: utf-8 -*-
import io
import datetime
import functools
import itertools
import time

import psycopg2
import pytz

from openerp import models, api, _
from openerp.tools import DEFAULT_SERVER_DATE_FORMAT, DEFAULT_SERVER_DATETIME_FORMAT, ustr

REFERENCING_FIELDS = set([None, 'id', '.id'])
def only_ref_fields(record):
    return dict((k, v) for k, v in record.items()
                if k in REFERENCING_FIELDS)
def exclude_ref_fields(record):
    return dict((k, v) for k, v in record.items()
                if k not in REFERENCING_FIELDS)

CREATE = lambda values: (0, False, values)
UPDATE = lambda id, values: (1, id, values)
DELETE = lambda id: (2, id, False)
FORGET = lambda id: (3, id, False)
LINK_TO = lambda id: (4, id, False)
DELETE_ALL = lambda: (5, False, False)
REPLACE_WITH = lambda ids: (6, False, ids)

class ImportWarning(Warning):
    """ Used to send warnings upwards the stack during the import process """
    pass

class ConversionNotFound(ValueError): pass


class ir_fields_converter(models.Model):
    _name = 'ir.fields.converter'

    @api.model
    def _format_import_error(self, error_type, error_msg, error_params=(), error_args=None):
        # sanitize error params for later formatting by the import system
        sanitize = lambda p: p.replace('%', '%%') if isinstance(p, str) else p
        if error_params:
            if isinstance(error_params, str):
                error_params = sanitize(error_params)
            elif isinstance(error_params, dict):
                error_params = dict((k, sanitize(v)) for k, v in error_params.items())
            elif isinstance(error_params, tuple):
                error_params = tuple(map(sanitize, error_params))
        return error_type(error_msg % error_params, error_args)

    @api.model
    def for_model(self, model, fromtype=str):
        """ Returns a converter object for the model. A converter is a
        callable taking a record-ish (a dictionary representing an openerp
        record with values of typetag ``fromtype``) and returning a converted
        records matching what :meth:`openerp.osv.orm.Model.write` expects.

        :param model: :class:`openerp.osv.orm.Model` for the conversion base
        :returns: a converter callable
        :rtype: (record: dict, logger: (field, error) -> None) -> dict
        """
        # make sure model is new api
        model = self.env[model._name]

        converters = {
            name: self.to_field(model, field, fromtype)
            for name, field in model._fields.items()
        }

        def fn(record, log):
            converted = {}
            for field, value in record.items():
                if field in (None, 'id', '.id'):
                    continue
                if not value:
                    converted[field] = False
                    continue
                try:
                    converted[field], ws = converters[field](value)
                    for w in ws:
                        if isinstance(w, str):
                            # wrap warning string in an ImportWarning for
                            # uniform handling
                            w = ImportWarning(w)
                        log(field, w)
                except ValueError as e:
                    log(field, e)
            return converted

        return fn

    @api.model
    def to_field(self, model, field, fromtype=str):
        """ Fetches a converter for the provided field object, from the
        specified type.

        A converter is simply a callable taking a value of type ``fromtype``
        (or a composite of ``fromtype``, e.g. list or dict) and returning a
        value acceptable for a write() on the field ``field``.

        By default, tries to get a method on itself with a name matching the
        pattern ``_$fromtype_to_$field.type`` and returns it.

        Converter callables can either return a value and a list of warnings
        to their caller or raise ``ValueError``, which will be interpreted as a
        validation & conversion failure.

        ValueError can have either one or two parameters. The first parameter
        is mandatory, **must** be a unicode string and will be used as the
        user-visible message for the error (it should be translatable and
        translated). It can contain a ``field`` named format placeholder so the
        caller can inject the field's translated, user-facing name (@string).

        The second parameter is optional and, if provided, must be a mapping.
        This mapping will be merged into the error dictionary returned to the
        client.

        If a converter can perform its function but has to make assumptions
        about the data, it can send a warning to the user through adding an
        instance of :class:`~.ImportWarning` to the second value
        it returns. The handling of a warning at the upper levels is the same
        as ``ValueError`` above.

        :param field: field object to generate a value for
        :type field: :class:`openerp.fields.Field`
        :param fromtype: type to convert to something fitting for ``field``
        :type fromtype: type | str
        :param context: openerp request context
        :return: a function (fromtype -> field.write_type), if a converter is found
        :rtype: Callable | None
        """
        assert isinstance(fromtype, (type, str))
        # FIXME: return None
        typename = fromtype.__name__ if isinstance(fromtype, type) else fromtype
        converter = getattr(self, '_%s_to_%s' % (typename, field.type), None)
        if not converter:
            return None
        return functools.partial(converter, model, field)

    @api.model
    def _str_to_boolean(self, model, field, value):
        # all translatables used for booleans
        true, yes, false, no = _("true"), _("yes"), _("false"), _("no")
        # potentially broken casefolding? What about locales?
        trues = set(word.lower() for word in itertools.chain(
            ['1', "true", "yes"], # don't use potentially translated values
            self._get_translations(['code'], "true"),
            self._get_translations(['code'], "yes"),
        ))
        if value.lower() in trues:
            return True, []

        # potentially broken casefolding? What about locales?
        falses = set(word.lower() for word in itertools.chain(
            ['', "0", "false", "no"],
            self._get_translations(['code'], "false"),
            self._get_translations(['code'], "no"),
        ))
        if value.lower() in falses:
            return False, []

        return True, [self._format_import_error(
            ImportWarning,
            _("Unknown value '%s' for boolean field '%%(field)s', assuming '%s'"),
            (value, yes),
            {'moreinfo': _("Use '1' for yes and '0' for no")}
        )]

    @api.model
    def _str_to_integer(self, model, field, value):
        try:
            return int(value), []
        except ValueError:
            raise self._format_import_error(
                ValueError,
                _("'%s' does not seem to be an integer for field '%%(field)s'"),
                value
            )

    @api.model
    def _str_to_float(self, model, field, value):
        try:
            return float(value), []
        except ValueError:
            raise self._format_import_error(
                ValueError,
                _("'%s' does not seem to be a number for field '%%(field)s'"),
                value
            )

    @api.model
    def _str_id(self, model, field, value):
        return value, []

    _str_to_reference = _str_to_char = _str_to_text = _str_to_binary = _str_to_html = _str_id

    @api.model
    def _str_to_date(self, model, field, value):
        try:
            time.strptime(value, DEFAULT_SERVER_DATE_FORMAT)
            return value, []
        except ValueError:
            raise self._format_import_error(
                ValueError,
                _("'%s' does not seem to be a valid date for field '%%(field)s'"),
                value,
                {'moreinfo': _("Use the format '%s'") % "2012-12-31"}
            )

    @api.model
    def _input_tz(self):
        # if there's a tz in context, try to use that
        if self._context.get('tz'):
            try:
                return pytz.timezone(self._context['tz'])
            except pytz.UnknownTimeZoneError:
                pass

        # if the current user has a tz set, try to use that
        user = self.env.user
        if user.tz:
            try:
                return pytz.timezone(user.tz)
            except pytz.UnknownTimeZoneError:
                pass

        # fallback if no tz in context or on user: UTC
        return pytz.UTC

    @api.model
    def _str_to_datetime(self, model, field, value):
        try:
            parsed_value = datetime.datetime.strptime(
                value, DEFAULT_SERVER_DATETIME_FORMAT)
        except ValueError:
            raise self._format_import_error(
                ValueError,
                _("'%s' does not seem to be a valid datetime for field '%%(field)s'"),
                value,
                {'moreinfo': _("Use the format '%s'") % "2012-12-31 23:59:59"}
            )

        input_tz = self._input_tz()# Apply input tz to the parsed naive datetime
        dt = input_tz.localize(parsed_value, is_dst=False)
        # And convert to UTC before reformatting for writing
        return dt.astimezone(pytz.UTC).strftime(DEFAULT_SERVER_DATETIME_FORMAT), []

    @api.model
    def _get_translations(self, types, src):
        types = tuple(types)
        # Cache translations so they don't have to be reloaded from scratch on
        # every row of the file
        tnx_cache = self._cr.cache.setdefault(self._name, {})
        if tnx_cache.setdefault(types, {}) and src in tnx_cache[types]:
            return tnx_cache[types][src]

        Translations = self.env['ir.translation']
        tnx = Translations.search([('type', 'in', types), ('src', '=', src)])
        result = tnx_cache[types][src] = [t.value for t in tnx if t.value is not False]
        return result

    @api.model
    def _str_to_selection(self, model, field, value):
        # get untranslated values
        env = self.with_context(lang=None).env
        selection = field.get_description(env)['selection']

        for item, label in selection:
            label = ustr(label)
            labels = [label] + self._get_translations(('selection', 'model', 'code'), label)
            if value == str(item) or value in labels:
                return item, []

        raise self._format_import_error(
            ValueError,
            _("Value '%s' not found in selection field '%%(field)s'"),
            value,
            {'moreinfo': [_label or str(item) for item, _label in selection if _label or item]}
        )

    @api.model
    def db_id_for(self, model, field, subfield, value):
        """ Finds a database id for the reference ``value`` in the referencing
        subfield ``subfield`` of the provided field of the provided model.

        :param model: model to which the field belongs
        :param field: relational field for which references are provided
        :param subfield: a relational subfield allowing building of refs to
                         existing records: ``None`` for a name_get/name_search,
                         ``id`` for an external id and ``.id`` for a database
                         id
        :param value: value of the reference to match to an actual record
        :param context: OpenERP request context
        :return: a pair of the matched database identifier (if any), the
                 translated user-readable name for the field and the list of
                 warnings
        :rtype: (ID|None, unicode, list)
        """
        id = None
        warnings = []
        action = {'type': 'ir.actions.act_window', 'target': 'new',
                  'view_mode': 'tree,form', 'view_type': 'form',
                  'views': [(False, 'tree'), (False, 'form')],
                  'help': _("See all possible values")}
        if subfield is None:
            action['res_model'] = field.comodel_name
        elif subfield in ('id', '.id'):
            action['res_model'] = 'ir.model.data'
            action['domain'] = [('model', '=', field.comodel_name)]

        RelatedModel = self.env[field.comodel_name]
        if subfield == '.id':
            field_type = _("database id")
            try: tentative_id = int(value)
            except ValueError: tentative_id = value
            try:
                if RelatedModel.search([('id', '=', tentative_id)]):
                    id = tentative_id
            except psycopg2.DataError:
                # type error
                raise self._format_import_error(
                    ValueError,
                    _("Invalid database id '%s' for the field '%%(field)s'"),
                    value,
                    {'moreinfo': action})
        elif subfield == 'id':
            field_type = _("external id")
            if '.' in value:
                xmlid = value
            else:
                xmlid = "%s.%s" % (self._context.get('_import_current_module', ''), value)
            try:
                id = self.env.ref(xmlid).id
            except ValueError:
                pass # leave id is None
        elif subfield is None:
            field_type = _("name")
            ids = RelatedModel.name_search(name=value, operator='=')
            if ids:
                if len(ids) > 1:
                    warnings.append(ImportWarning(
                        _("Found multiple matches for field '%%(field)s' (%d matches)")
                        % (len(ids))))
                id, _name = ids[0]
        else:
            raise self._format_import_error(
                Exception,
                _("Unknown sub-field '%s'"),
                subfield
            )

        if id is None:
            raise self._format_import_error(
                ValueError,
                _("No matching record found for %(field_type)s '%(value)s' in field '%%(field)s'"),
                {'field_type': field_type, 'value': value},
                {'moreinfo': action})
        return id, field_type, warnings

    def _referencing_subfield(self, record):
        """ Checks the record for the subfields allowing referencing (an
        existing record in an other table), errors out if it finds potential
        conflicts (multiple referencing subfields) or non-referencing subfields
        returns the name of the correct subfield.

        :param record:
        :return: the record subfield to use for referencing and a list of warnings
        :rtype: str, list
        """
        # Can import by name_get, external id or database id
        fieldset = set(record.keys())
        if fieldset - REFERENCING_FIELDS:
            raise ValueError(
                _("Can not create Many-To-One records indirectly, import the field separately"))
        if len(fieldset) > 1:
            raise ValueError(
                _("Ambiguous specification for field '%(field)s', only provide one of name, external id or database id"))

        # only one field left possible, unpack
        [subfield] = fieldset
        return subfield, []

    @api.model
    def _str_to_many2one(self, model, field, values):
        # Should only be one record, unpack
        [record] = values

        subfield, w1 = self._referencing_subfield(record)

        reference = record[subfield]
        id, _, w2 = self.db_id_for(model, field, subfield, reference)
        return id, w1 + w2

    @api.model
    def _str_to_many2many(self, model, field, value):
        [record] = value

        subfield, warnings = self._referencing_subfield(record)

        ids = []
        for reference in record[subfield].split(','):
            id, _, ws = self.db_id_for(model, field, subfield, reference)
            ids.append(id)
            warnings.extend(ws)
        return [REPLACE_WITH(ids)], warnings

    @api.model
    def _str_to_one2many(self, model, field, records):
        commands = []
        warnings = []

        if len(records) == 1 and exclude_ref_fields(records[0]) == {}:
            # only one row with only ref field, field=ref1,ref2,ref3 as in
            # m2o/m2m
            record = records[0]
            subfield, ws = self._referencing_subfield(record)
            warnings.extend(ws)
            # transform [{subfield:ref1,ref2,ref3}] into
            # [{subfield:ref1},{subfield:ref2},{subfield:ref3}]
            records = ({subfield:item} for item in record[subfield].split(','))

        def log(_, e):
            if not isinstance(e, Warning):
                raise e
            warnings.append(e)

        convert = self.for_model(self.env[field.comodel_name])

        for record in records:
            id = None
            refs = only_ref_fields(record)
            # there are ref fields in the record
            if refs:
                subfield, w1 = self._referencing_subfield(refs)
                warnings.extend(w1)
                reference = record[subfield]
                id, _, w2 = self.db_id_for(model, field, subfield, reference)
                warnings.extend(w2)

            writable = convert(exclude_ref_fields(record), log)
            if id:
                commands.append(LINK_TO(id))
                commands.append(UPDATE(id, writable))
            else:
                commands.append(CREATE(writable))

        return commands, warnings
