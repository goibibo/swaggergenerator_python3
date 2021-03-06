import numbers
import re
from collections import defaultdict, namedtuple
from urllib.parse import parse_qsl

import ast
import json

import flex.exceptions
import flex.http
from flex.core import validate
import collections

from . import paths
from .yaml import get_yaml  # noqa


class EmptyExampleArrayError(ValueError):
    """Raised when an empty array is seen during generation.

    An 'items' key for all arrays is required in OAS, but we can't safely guess the subschema
    from an empty array.
    """
    pass


class Example(namedtuple('Example', ['request', 'response', 'description', 'summary'])):
    """A sample api interaction.

    Attributes:
        request: a flex.http.Request
        response: a flex.http.Response
        description: python description.
        summary: name of the docs.
    """

    def __repr__(self):
        return "'%s %s -> %s'" % (self.request.method.lower(), self.response.path,
                                  self.response.status_code)

    __str__ = __repr__


class Generator(object):
    """A Generator stores examples and can output schemas to match them."""

    _param_pattern = re.compile(r'^{.+}$')

    def __init__(self, base_path='', existing_schema=None, default=None, query_key_blacklist=None):
        """

        Args:
            base_path: when provided, generated paths will omit this prefix.
            existing_schema:  the result of
              `flex.load <http://flex-swagger.readthedocs.org/en/latest/#supported-schema-formats>`__
              for an api schema. Useful when adding paths to an existing schema.
              When provided, any definitions that match a generated schema will be replaced with a $ref reference.
            default: a dictionary representing a schema. Useful when your paths return a uniform error schema.
              When provided, this will be set as the "default" response for any generated paths.
            query_key_blacklist: a set of querystring param names to ignore. Useful when the same param (like a token)
              is passed in examples, but should not be generated in schemas.
        """

        if existing_schema is None:
            existing_schema = {}

        if query_key_blacklist is None:
            query_key_blacklist = set()

        self.base_path = base_path
        self.existing_schema = existing_schema
        self.default = default
        self.query_key_blacklist = query_key_blacklist

        self.path_to_examples = defaultdict(list)

    def is_param(self, ex, path):
        """Determine if ex is a parameter in this path.

        By default, this will match path templates and all-digit strings.
        Override to customize this behavior.

        Args:
            ex: the piece of the path in question
            path: the entire path

        Returns:
            a truthy value is ex is a param, else a falsey value.
        """
        return self._param_pattern.match(ex) or ex.isdigit()

    def normalize_example(self, example):
        """Override to perform custom modification to an Example
        after it's been normalized by flex.

        Mutating ``example`` is permitted (but it should still be returned).

        Returns:
            an Example.
        """

        return example

    def provide_example(self, request, response, description='', summary=''):
        """Store an example interaction to use later in generation.

        If ``existing_schema`` was provided and the interaction matches an existing path/verb pair,
        it will be ignored.

        Request and response are normalized by flex.
        A complete list of supported types can be found `in the flex docs
        <http://flex-swagger.readthedocs.org/en/latest/#api-call-validation>`__.

        Args:
            request: a request object recognized by flex
            response: a response object recognized by flex
            description: a string for function mentioned.
        """
        flex_response = flex.http.normalize_response(response, request)

        example = Example(flex_response.request, flex_response, description, summary)
        example = self.normalize_example(example)

        if not self.existing_schema or not self._known_to_schema(example):
            self.path_to_examples[flex_response.path].append(example)

    def generate_paths(self):
        """Generate a swagger `Paths Object <http://swagger.io/specification/#pathsObject>`__ from
        any previously-provided examples.

        Returns:
            a dictionary mapping template paths to dictionaries representing
            `Path Item Objects <http://swagger.io/specification/#pathItemObject>`__.

        To output yaml for pasting into an existing schema, call
        :func:`get_yaml <swaggergenerator3.get_yaml>` on the result.
        """

        valid_examples = self._merge_examples(self.path_to_examples)

        schemas = {path: self._generate_path(path, exs) for (path, exs) in valid_examples.items()}

        for path, schema in schemas.items():
            for example in (ex for ex in valid_examples[path] ):
                            # if ex.response.status_code.startswith('2')):
                verb = example.request.method.lower()
                if example.response.status_code in schema[verb]['responses']:
                    response = schema[verb]['responses'][example.response.status_code]
                    response['schema'] = self._match_references(response['schema'],
                                                                example.response.data)

                    matched_params = []
                    for param in schema[verb]['parameters']:
                        if param['in'] == 'body':
                            param['schema'] = self._match_references(param['schema'],
                                                                     example.request.data)
                        matched_params.append(param)
                    schema[verb]['parameters'] = matched_params

        return schemas

    def _known_to_schema(self, example):
        known = False
        example_components = self._get_components(example.response.path)

        for known_path, verbs in self.existing_schema.get('paths', {}).items():
            if ((paths.component_matches(example_components, self._get_components(known_path))
                 and example.request.method.lower() in verbs)):
                known = True
                break

        return known

    def _merge_examples(self, path_to_examples):
        # return a copy of path_to_examples, but with paths that are likely the same
        # (just with different url params) merged into one parameterized path

        # Merging urls is currently a two-step process.
        # First, we detect "naive" params: those that is_param returns true for.
        # Second, we take the naive patterns and try to match them against the others.

        # Here's an example of how this works:
        # Given urls:
        #   1) /users/1
        #   2) /users/test_an_error
        # On the first pass, we find these naive patterns:
        #   1) ('users', None)
        #   2) ('users', 'test_an_error')
        # On the second pass, we find that the 2) is a match for 1), and merge those.
        # 1) is _not_ a match for 2). If it was, we'd end up merging our more generic urls into
        # more specific ones.

        # Detect naive params.
        component_examples = defaultdict(list)
        for path, exs in path_to_examples.items():
            components = self._get_components(path)
            component_examples[components].extend(exs)

        # Use naive param patterns to detect non-naive params.
        for c1 in component_examples.copy():
            match = None

            for c2 in component_examples:
                if c1 is c2:
                    continue
                if paths.component_matches(c1, c2):
                    match = c2
                    break

            if match:
                my_examples = component_examples.pop(c1)
                component_examples[match].extend(my_examples)

        return {
            paths.build_paramaterized_path(components): exs
            for (components, exs) in component_examples.items()
        }

    def _get_components(self, path):
        components = tuple(
            [e if not self.is_param(e, path) else None
             for e in path.split('/')])

        base_components = tuple(self.base_path.split('/'))
        if base_components == components[:len(base_components)]:
            components = components[len(base_components):]

        if components[0] == '':
            components = components[1:]

        return components

    def _generate_path(self, path, examples):
        # return a dict representing a schema for this example
        operation = lambda: {'responses': {}, 'description': 'TODO'}
        schema = defaultdict(operation)

        for ex in examples:
            verb = ex.request.method.lower()
            schema[verb]['description'] = ex.description or 'TODO'
            schema[verb]['summary'] = ex.summary or ''
            if ex.response.content_type == 'application/json':
                try:
                    schema[verb]['responses'][ex.response.status_code] = \
                        self._generate_response_params(
                            ex.response.data,
                            schema[verb]['responses'].get(ex.response.status_code, {}))
                except EmptyExampleArrayError:
                    pass

                # TODO this can leave out path params. probably in the case
                # where the example is a non-naive param, since we call get_components
                # and maybe the path hasn't been replaced with a templated one?
                schema[verb]['parameters'] = self._generate_request_params(
                    path, ex.request, schema[verb].get('parameters', []))

            if self.default is not None:
                schema[verb]['responses']['default'] = self.default

        # pyyaml cannot accept defaultdicts.
        return dict(schema)

    def _generate_response_params(self, response, params=None):
        ret = {
            'schema': self._generate_schema(response),
            'description': 'TODO'
        }
        if params:
            ret['schema'] = self._merge_response_schema(ret['schema'], params['schema'])
        return ret

    def _get_request_query_params_with_data_type(self, query_items):
        """Determine data type for request query params."""
        new_query_dict = {}
        for k, v in query_items:
            try:
                v = ast.literal_eval(v)
            except Exception:
                try:
                    v = json.loads(v)
                except Exception:
                    pass
            new_query_dict[k] = v
        return new_query_dict

    def _generate_request_params(self, path, request, params=None):
        parameters = []

        # TODO we could also detect other param types easily. currently we just do strings.
        query_dict = parse_qsl(request.query)
        modified_req_params = self._get_request_query_params_with_data_type(query_dict)
        for q_k, q_v in modified_req_params.items():
            query_param = {
                'name': q_k,
                'in': 'query',
                'required': True,
            }
            schema_type = self._generate_type_params(q_v)

            query_param.update(schema_type)

            if q_k not in self.query_key_blacklist:
                parameters.append(query_param)

        i = 0
        for c in self._get_components(path):
            if c is None:
                i += 1
                path_param = {
                    'name': "param%s" % i,
                    'in': 'path',
                    'required': True,
                    'type': 'string',
                }
                parameters.append(path_param)

        if request.data:
            body_param = {
                'name': 'body_data',
                'in': 'body',
                'schema': self._generate_schema(request.data),
            }
            parameters.append(body_param)
        if params:
            parameters = self._merge_query_params(parameters, params)
        return parameters

    @staticmethod
    def _merge_query_params(params1, params2):
        # create dict of query_params2
        query_params2 = {
            param['name']: param for param in params2 if param['in'] == 'query'}

        for param in params1:
            if param['in'] == 'query':
                if param['name'] in query_params2:
                    query_params2.pop(param['name'])
                else:
                    param['required'] = False
        for name, param in query_params2.items():
            param['required'] = False
            params1.append(param)

        return params1

    @staticmethod
    def _get_swagger_type(x):
        # see https://github.com/OAI/OpenAPI-Specification/blob/master/versions/2.0.md#data-types.

        # OAS doesn't accept null values, but json-schema (and flex) accept the string "null".
        _type = 'null'

        if isinstance(x, dict):
            _type = 'object'
        elif x is True or x is False:
            # tricky: booleans are subtypes of integers.
            _type = 'boolean'
        elif isinstance(x, numbers.Number):
            _type = 'number'
        elif isinstance(x, list):
            _type = 'array'
        elif isinstance(x, str):
            _type = 'string'

        return _type

    def _generate_type_params(self, body):
        _type = self._get_swagger_type(body)
        schema = {"type": _type}
        if _type == 'object':
            properties = {
                key: self._generate_type_params(val)
                          for key, val in body.items()}
            schema['properties'] = properties
        elif _type == 'array':
            if len(body) == 0:
                raise EmptyExampleArrayError

            # note that this will cause problems with non-homogeneous arrays
            schema['items'] = self._generate_type_params(body[0])

        return schema

    def _generate_schema(self, body):
        _type = self._get_swagger_type(body)

        schema = {
            'type': _type,
        }

        if _type == 'object':
            schema['additionalProperties'] = False
            properties = {key: self._generate_schema(val)
                          for key, val in body.items()}
            schema['properties'] = properties
        elif _type == 'array':
            if len(body) == 0:
                raise EmptyExampleArrayError

            # note that this will cause problems with non-homogeneous arrays
            schema['items'] = self._generate_schema(body[0])

        return schema

    def _merge_response_schema(self, schema1, schema2):
        schema1 = paths.update_dict(schema1, schema2)
        return schema1

    def _match_references(self, schema, body):
        if '$ref' in schema:
            pass
        elif schema['type'] == 'array' and 'items' in schema and len(body) > 0:
            # Recurse into arrays.
            schema['items'] = self._match_references(schema['items'], body[0])
        elif schema['type'] == 'object' and 'properties' in schema:
            if body == {}:
                # Don't match the empty object against loose definitions.
                return schema

            # Try to match the current object against a definition.
            for name, definition in self.existing_schema.get('definitions', {}).items():
                try:
                    validate(definition, body, context=self.existing_schema)
                except flex.exceptions.ValidationError:
                    pass
                else:
                    return {'$ref': "#/definitions/%s" % name}

            # No definition matched; recurse into subschemas.
            for prop, prop_schema in schema['properties'].items():
                if prop in body:
                    schema['properties'][prop] = self._match_references(prop_schema, body[prop])

        return schema
