# Copyright 2019 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import copy
from datetime import datetime
import logging
import os
import re
import subprocess
import traceback
import uuid

from sampletester import testenv


class TestCase:
  # The kwarg/YAML list element key containing a custom message for the various
  # functions testing for string inclusion/exclusion in previous output.
  KEY_CONTAINS_MESSAGE='message'

  def __init__(self, environment: testenv.Base,
               idx: int, label: str,
               setup, case, teardown):
    self.failures = []
    self.errors = []
    self.output = ""

    self.environment = environment
    self.idx = idx
    self.label = label
    self.setup = setup
    self.case = case
    self.teardown = teardown

    self.last_return_code = 0
    self.last_call_output = ""
    self.start_time = None
    self.end_time = None

    # The key is the external binding available through `code` and directly through yaml keys.
    #
    # The value is a pair. The first element is the test variable or
    # function. The second element, if not None, is the "yaml_prep" function
    # that returns a pair of ([arguments], {kwargs}) for passing to the function; if
    # the yaml_prep returns None, the test function is not called (this is
    # useful to provide an alternate representation in the YAML)
    self.builtins = {
        ### Variables: meta info about the test case, last output
        "testcase_num": (self.idx, None),
        "testcase_id": (self.label, None),
        "_last_call_output": (self.last_call_output, None),

        ### Functions to execute processes
        "call": (self.call_no_error, self.params_for_call),
        "call_may_fail": (self.call_allow_error, self.params_for_call),
        "shell": (self.shell, self.yaml_args_string),

        ### Other functions available to the test suite
        "uuid": (self.get_uuid, self.yaml_get_uuid),
        "env": (self.get_env, self.yaml_get_env),
        "log": (self.print_out, self.yaml_args_string),
        "extract_match": (self.extract_match, self.yaml_extract_match),

        ### Code
        "code": (self.execute, lambda p: ([p], {})),

        # Functions to fail the test: these are intended for code only
        "fail": (self.fail, None),
        "expect": (self.expect, None),
        "abort": (self.abort, None),
        "assert_that": (self.assert_that, None),

        ### Functions to fail the test: intended for YAML, may be used in code as well.
        # contains any of a list
        "assert_contains_any": (self.contain_checker(self.assert_that, any, True),
                                self.params_for_contains),
        # does not contain any of a list (all list elements absent)
        "assert_excludes": (self.contain_checker(self.assert_that, all, False),
                                self.params_for_contains),
        # alias for "assert_excludes"
        "assert_not_contains": (self.contain_checker(self.assert_that, all, False),
                                self.params_for_contains),

        # contains all of a list
        "assert_contains": (self.contain_checker(self.assert_that, all, True),
                                self.params_for_contains),
        # does not contain some of the list (at least one list element absent)
        "assert_excludes_any": (self.contain_checker(self.assert_that, any, False),
                                self.params_for_contains),
        "assert_success": (self.assert_success, self.yaml_args_string),
        "assert_failure": (self.assert_failure, self.yaml_args_string),
        # Due to feedback in the spec, we only allow assert_ functions (which exit
        # the test case immediately) and not expect_ functions (which would allow
        # the test to continue even if an expectation is not met).
        # "expect_contains_any": (self.# contain_checker(self.expect, any, True),
        #                         self.params_for_contains),
        # "expect_not_contains": (self.contain_checker(self.expect, all, False),
        #                         self.params_for_contains),
        # "expect_contains": (self.contain_checker(self.expect, all, True),
        #                     self.params_for_contains),
        # "expect_not_contains_some": (self.contain_checker(self.expect, any, False),
        #                              self.params_for_contains),


    }

    self.local_symbols = {}
    for symbol, info in self.builtins.items():
      self.local_symbols[symbol] = info[0]

  def get_failures(self):
    return [(status, message.format(*args))
            for status, message, args in self.failures]

  def get_errors(self):
    return [
        (status, message.format(*args)) for status, message, args in self.errors
    ]

  def record_failure(self, status, message, *args):
    """Records a single failure in this TestCase."""
    self.failures.append((status, message, args))

  def record_error(self, status, message, *args):
    """Records a single error in this TestCase."""
    self.errors.append((status, message, args))

  def expect(self, condition, message, *args):
    """Expects condition or records failure."""
    if not condition:
      self.record_failure("FAILED EXPECTATION", message, *args)
      self.print_out("# FAILED EXPECTATION", message, *args)

  def fail(self):
    """Explicitly fails the test."""
    self.expect(False, "failure")

  def assert_that(self, condition, message, *args):
    """Asserts condition or records failure, soft-aborting the test."""
    if not condition:
      self.record_failure("FAILED ASSERTION", message, *args)
      self.print_out("# FAILED ASSERTION: " + message, *args)
      raise TestFailure

  def abort(self):
    """Explicitly fails and soft-aborts the test."""
    self.assert_that(False, "abort called")

  def print_out(self, msg, *args):
    """Formats `msg` according to `args` and records it in the TestCase output."""
    try:
      self.output += self.format_string(str(msg), *args) + "\n"
    except Exception as e:
      raise

  def execute(self, code):
    """Executes YAML directive "code"."""
    exec(code, None, self.local_symbols)

  def get_uuid(self):
    """Gets a UUID via code."""
    return str(uuid.uuid4())

  def yaml_get_uuid(self, var_name):
    """Gets a UUID via YAML."""
    self.local_symbols[var_name] = self.get_uuid()
    return None, None

  def get_env(self, env_var):
    """Gets an environment variable via code."""
    # TODO: Catch key error
    return os.environ[env_var]

  def yaml_get_env(self, parts):
    """Gets an environment variable via YAML."""
    var_name, env_var = self.params_for_set(parts)
    self.local_symbols[var_name] = self.get_env(env_var)
    return None, None

  def extract_match(self, pattern, variable=None, group_variables=None):
    """Extracts regular expression captures from output via code."""

    if not pattern:
      raise ConfigError("extract_match requires pattern to match")
    if not variable and not group_variables:
      raise ConfigError("extract_match requires variable or groups")
    if variable and group_variables:
      raise ConfigError("extract_match cannot accept both variables and groups")

    # Add all variable names to local_symbols (None is OK value if no match)
    self.local_symbols[variable] = None
    if group_variables:
      for variable_name in group_variables:
       self.local_symbols[variable_name] = None

    text = self.last_call_output
    match = re.search(pattern, text)
    if match and match.groups():
      captures = match.groups()
      if variable:
        self.local_symbols[variable] = captures[0]
      if group_variables:
        for idx, variable_name in enumerate(group_variables):
          if len(captures) > idx:
            self.local_symbols[variable_name] = captures[idx]
    return None, None

  def yaml_extract_match(self, parts):
    """Extracts regular expression captures from output via code."""
    key_pattern = 'pattern'
    key_variable = 'variable'
    key_groups = 'groups'
    return [parts.get(key_pattern), parts.get(key_variable),
      parts.get(key_groups)], None

  def call_allow_error(self, *args, **kwargs):
    """Invokes `cmd` (formatted with `params`). Does not fail in case of error."""
    try:
      call, chdir = self.environment.get_call(*args, **kwargs)
    except Exception as e:
      raise CallError('could not resolve call: {}'.format(str(e)))
    return self._call_external(call, chdir)

  def shell(self, cmd, *args):
    return self._call_external(self.format_string(cmd + " {}"*len(args), *args))

  def _call_external(self, cmd, chdir=None):
    self.last_return_code = 0
    self.last_call_output = ""

    try:
      self.print_out("\n# Calling: " + cmd)
      out = subprocess.check_output(cmd, stderr=subprocess.STDOUT, shell=True, cwd=chdir)
      return_code = 0
    except subprocess.CalledProcessError as e:
      return_code = e.returncode
      out = e.output
      # TODO(vchudnov): Prefix the error output with comments
      self.output += "# ... call did not succeed  "
      # return return_code, out
    except Exception as e:
      raise

    new_output = out.decode("utf-8")
    self.last_return_code = return_code
    # TODO: De-dupe the following. Either some accessor magic, or have it live in local_symbols
    self.last_call_output = new_output
    self.local_symbols['_last_call_output'] = new_output

    self.output += new_output
    return return_code, new_output

  def call_no_error(self, *args, **kwargs):
    """Invokes `cmd` (formatted with `args`), failing/soft-aborting if error."""
    return_code, out = self.call_allow_error(*args, **kwargs)
    self.assert_that(return_code == 0, 'call failed: "{}"', args)
    return out

  def contain_checker(self, check, which, contains: bool):
    """Returns a function to check for string presence in the last output

    This method returns a function that will check whether any or all strings in
    a collection are present or absent in the last output.

    Args:
      check: a function to determine how to perform the check, either
        `self.assert_that` or `self.expect`
      which: a function to determine how to deal with the condition when
        checking against multiple values: either `all` (all the values must meet
        the condition) or `any` (at least one must meet the condition)
      contains: if True, the condition tests for inclusion; if False, it tests
        for exclusion

    Returns:
      A function that can be passed with `*values` to check against the last output,
      and `**kwargs` to control operation
    """
    def checker(*values, **kwargs):
      """Checks whether any/all of value are included or excluded from last_output

      Args:
        values: The values to be tested for inclusion/exclusion
        kwargs: The settings for the inclusion check. This function only looks
          at the argument with name given by KEY_CONTAINS_MESSAGE, and uses that
          as the message if the overall check on the collection of `values`
          fails. The rest of the `kwargs` map is passed to
          `self.last_output_contains`.
      """
      message = ''

      if self.KEY_CONTAINS_MESSAGE in kwargs:
        message = kwargs[self.KEY_CONTAINS_MESSAGE]
        del(kwargs[self.KEY_CONTAINS_MESSAGE])
      if contains:
        condition = lambda substr: self.last_output_contains(substr, **kwargs)
      else:
        condition = lambda substr: not self.last_output_contains(substr, **kwargs)
      self._check_several(check, which, condition, message, values)
    return checker

  # Assertion on the return value of the last call indicating success.
  def assert_success(self, message=[], *args):
    mesage = message or "expected last call to succeed"
    self.assert_that(self.last_return_code == 0, message, *args)

  # Assertion on the return value of the last call indicating failure.
  def assert_failure(self, message=[], *args):
    message = message or "expected last call to fail"
    self.assert_that(self.last_return_code != 0, message, *args)

  def _check_several(self, check, which, condition, message, values):
    """Utility function for the `expect_*` and `assert_*` calls.

    Runs `check` on `condition` for each element of `values`, reporting on
    aggregate errors (as determined by `which`) either with the default error
    message or `message`, if non- empty.

    Args:
      check: one of `self.assert`_that or `self.expect`
      which: one of `any` or `all`
      condition: a function that evaluates to true in the expected case
      message: if non-empty, the message to output if the condition fails. If
         empty, a default message will be constructed
      values: the values to be tested using condition. This check will pass or
         fail depending on the result of `which` applies to the result of
         `condition` on these `values`.
    """
    default_message = len(message) == 0
    label_check = 'required' if check == self.assert_that else 'expected' if check == self.expect else None
    label_which = 'all of' if which == all else 'any of' if which == any else None
    if not label_check or not label_which:
      raise Exception('`check` must be `self.assert` or `self.expect`; `which` must be `any` or `all`')

    if default_message:
      message = ('{}, but did not find, {} the following values in the preceding output: {}'
                 .format(label_check, label_which, values))
    check(which([condition(substr) for substr in values]), message)

  def run(self):
    self.start_time = datetime.now()
    status_message = ""
    log_entry_prefix = "---- Test case {:d}: \"{:s}\"".format(
        self.idx, self.label)

    def run_segments_of(stage_spec):
      for spec_segment in stage_spec:
        self.run_segment(spec_segment)

    try:
      for stage_name, stage_spec in [("SETUP", self.setup), ("TEST", self.case)]:
        self.print_out("\n### Test case {0}".format(stage_name))
        run_segments_of(stage_spec)
    except TestFailure:
      pass
    except CallError as e:
      status = "CALL ERROR in stage {} ".format(stage_name)
      self.record_error(status, e.msg)
      self.print_out(status + ": " + e.msg)

    except KeyboardInterrupt:
      status = "KEYBOARD INTERRUPT in stage {} ".format(stage_name)
      self.record_error(status, "keyboard interrupt detected")
      self.print_out(status)
      raise

    except Exception as e:
      status = "UNHANDLED EXCEPTION in stage {} ".format(stage_name)
      short_description = repr(e)
      description = short_description + "\n" + "".join( traceback.format_tb(e.__traceback__))
      self.record_error(status, description)
      self.print_out("# EXCEPTION!! " + short_description)

    finally:
      try:
        self.print_out("\n### Test case TEARDOWN")
        run_segments_of(self.teardown)
      except TestFailure:
        status = "unexpected TEST FAILURE in stage TEARDOWN"
        self.record_error(status, "test failure in stage TEARDOWN")
        self.print_out(status)
      except CallError as e:
        status = "CALL ERROR in stage TEARDOWN "
        self.record_error(status, e.msg)
        self.print_out(status + ": " + e.msg)
      except KeyboardInterrupt:
        status = "KEYBOARD INTERRUPT in stage TEARDOWN"
        self.record_error(status, "keyboard interrupt detected")
        self.print_out(status)
        raise
      except Exception as e:
        status = "UNHANDLED EXCEPTION in stage TEARDOWN"
        short_description = repr(e)
        description = short_description + "\n" + "".join( traceback.format_tb(e.__traceback__))
        self.record_error(status, description)
        self.print_out("# EXCEPTION!! " + short_description)

    print_output = True
    if len(self.failures) > 0:
      logging.info(log_entry_prefix + " FAILED --------------------")
      for failure in self.failures:
        logging.info("    {}: {}".format(
            failure[0], self.format_string(failure[1], *failure[2])))
        print_output = True
    elif len(self.errors) > 0:
      logging.info(log_entry_prefix + " ERRORED --------------------")
      for error in self.errors:
        logging.info("    {}: (check state: clean-up did not finish) {}".format(
            error[0], self.format_string(error[1], *error[2])))
        print_output = True
    else:
      logging.info(log_entry_prefix + " PASSED ------------------------------")
    if print_output:
      logging.info("    Output:")
      logging.info(self.get_output(4, "| ") + "\n")

    self.end_time = datetime.now()
    return len(self.failures) + len(self.errors)

  def get_output(self, indent=0, header=""):
    return reindent(copy.deepcopy(self.output), indent, header)

  def run_segment(self, spec_segment):
    if len(spec_segment) > 1:
      raise ConfigError

    for directive, segment in spec_segment.items():
      if directive not in self.builtins:
        raise ConfigError("unknown YAML directive: " + str(directive))

      howto = self.builtins[directive]
      if howto[1] == None:
        raise ConfigError("directive only available inside a code directive: " +
                          directive)

      args, kwargs = howto[1](segment)
      if args is None and kwargs is None:
        return
      args = args or []
      kwargs = kwargs or {}

      howto[0](*args, **kwargs)

  #### Helper methods

  def last_output_contains(self, substr, **kwargs):
    case_sensitive = kwargs.get('case_sensitive', False)
    if case_sensitive:
      return substr in self.last_call_output
    return substr.lower() in self.last_call_output.lower()

  def format_string(self, msg, *args):
    """Returns `msg` formatted with `*args`.

    This interpolates any environment symbols named by `{symbol_name}` with
    `msg`, interpolates arguments into `{}` sequences within `msg. It
    automatically adds any `{}` placeholders needed to match `len(args)`.
    """

    msg = interpolate_symbols(msg, self.environment.get_symbol)

    if len(args) == 0:
      return msg

    # TODO: Allow for escaping braces.
    count = msg.count("{}")

    # Add any missing placeholders
    missing = len(args) - count
    if missing > 0:
      msg = msg + ": " + "{} " * missing

    formatted = msg.format(*args)
    return formatted

  def yaml_args_string(self, parts):
    """Gets printf-style arguments from a YAML directive.

    Interprets `parts` as a list whose first element is a print string (without
    argument substitution) and whose subsequent elements are local symbol names
    or string literals.

    Returns the list with the names of the symbols substituted by their values
    in the self.local_symbols.
    """
    if parts is None or len(parts) == 0:
      return [], {}
    return [parts[0]] + self.lookup_values(parts[1:]), {}

  def params_for_set(self, parts):
    key_name = 'name'
    key_variable = 'variable'
    if len(parts) < 2:
      log_raise(
          logging.critical, ValueError,
          'need both "{}" and "{}"'
          .format(key_name, key_variable))
    return parts[key_variable], parts[key_name]

  def params_for_call(self, parts):
    key_cmd = self.environment.get_testcase_settings().get('call.target', 'target')
    key_params = "params"
    key_args = "args"
    if len(parts) < 1 or not key_cmd in parts:
      log_raise(
          logging.critical, ValueError,
          'when calling artifacts, the first parameter must be "- {}: TARGET"'
          .format(key_cmd))

    cmd = parts[key_cmd]
    params = {}
    args = []
    if len(parts) == 1:
      return [cmd], params

    for key, val in parts.items():
      if key == key_cmd:
        if val != cmd:
          log_raise(
              logging.critical, ValueError,
              'encountered multiple "- {}": "{}" vs "{}"'.format(
                  key_cmd, cmd, val))
        continue
      if key == key_params:
        for name, value in val.items():
          params[name] = self.get_variable_or_literal(value)
        continue
      if key == key_args:
        for value in val:
          args.append(self.get_variable_or_literal(value))
        continue
      log_raise(logging.critical, ValueError,
                'unknown argument to function call "- {}"'.format(key))
    return [cmd] + args, params

  def params_for_contains(self, parts):
    # TODO: Allow case-sensitive matching as well, once we figure out the format
    # in the YAML. We can probably use a similar format as we do for
    # KEY_CONTAINS_MESSAGE.
    params, message = self.string_and_params(TestCase.KEY_CONTAINS_MESSAGE, parts)
    return params, {'case_sensitive': False,
                    TestCase.KEY_CONTAINS_MESSAGE: message}

  def string_and_params(self, name_label: str, parts, *, strict: bool = False):
    if name_label in parts[0]:
      name_value = parts[0][name_label]
      start = 1
    else:
      if strict:
        log_raise(logging.critical, ValueError,
                  'expected field "{}"'.format(name_label))
      name_value = ''
      start = 0
    params = self.get_yaml_values(parts[start:])
    return params, name_value

  def get_yaml_values(self, list):
    """Gets values from the `list` of maps, each map containing at most the keys "variable" or "literal".

    Returns a list of the values of all the variables and literals specified.
    """
    values = []
    for map in list:
      values.append(self.get_variable_or_literal(map))
    return values

  def get_variable_or_literal(self, map):
    key_variable = 'variable'
    key_literal = 'literal'
    if len(map) > 1:
     log_raise(
          logging.critical, ValueError,
          'expected each element to contain only one of "{}", "{}", but got {}'
          .format(key_variable, key_literal, map))
    for type, item in map.items():
      if type == key_variable:
        item = self.local_symbols[item]
      elif type != key_literal:
        raise ConfigError(
            'expected "{}" or "{}", got "{}":""{}"'.format(
                key_variable, key_literal, type, item))
    return item

  def lookup_values(self, strings):
    """Returns a list containing variable values and/or literals.

    For each string in the input, the corresponding output element is either the
    value of the variable with that name, if such exists, or the quoted string
    itself..
    """
    return [self.local_symbols.get(p, '"{}"'.format(str(p))) for p in strings]

### Helpers for substituting symbol values

_interpolated_symbol_re = re.compile('{([^}]+)}')
def interpolate_symbols(msg, resolver):
  """Interpolates symbols in `msg`.

  Replaces any substring of the form "{sname}" in `msg` with the value returned
  by `resolver(sname)`. This one-liner is broken out into a separate function
  for easier testing.
  """
  return _interpolated_symbol_re.sub(lambda match: resolver(match.group(1)), msg)

### General helpers

class TestFailure(Exception):
  """Exception raised when a test fails (typically via an assertion)."""
  pass


class ConfigError(Exception):
  def __init__(self, msg):
    self.msg = msg

class CallError(Exception):
  def __init__(self, msg):
    self.msg = msg


# heavily adapted from from https://www.oreilly.com/library/view/python-cookbook/0596001673/ch03s12.html
def reindent(s, numSpaces, prompt):
  s = s.split("\n")
  s = [(numSpaces * " ") + prompt + line for line in s]
  s = "\n".join(s)
  return s


def log_raise(log_fn, exception, message):
  log_fn(message)
  raise exception(message)
