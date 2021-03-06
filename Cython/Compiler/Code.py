# cython: language_level = 3, py2_import=True
#
#   Code output module
#

import cython
cython.declare(re=object, Naming=object, Options=object, StringEncoding=object,
               Utils=object, SourceDescriptor=object, StringIOTree=object,
               DebugFlags=object, none_or_sub=object, basestring=object)

import re
import Naming
import Options
import StringEncoding
from Cython import Utils
from Scanning import SourceDescriptor
from Cython.StringIOTree import StringIOTree
import DebugFlags

from Cython.Utils import none_or_sub
try:
    from __builtin__ import basestring
except ImportError:
    from builtins import str as basestring


non_portable_builtins_map = {
    # builtins that have different names in different Python versions
    'bytes'         : ('PY_MAJOR_VERSION < 3',  'str'),
    'unicode'       : ('PY_MAJOR_VERSION >= 3', 'str'),
    'xrange'        : ('PY_MAJOR_VERSION >= 3', 'range'),
    'BaseException' : ('PY_VERSION_HEX < 0x02050000', 'Exception'),
    }

uncachable_builtins = [
    # builtin names that cannot be cached because they may or may not
    # be available at import time
    'WindowsError',
    ]

class UtilityCode(object):
    # Stores utility code to add during code generation.
    #
    # See GlobalState.put_utility_code.
    #
    # hashes/equals by instance

    is_cython_utility = False

    def __init__(self, proto=None, impl=None, init=None, cleanup=None, requires=None,
                 proto_block='utility_code_proto'):
        # proto_block: Which code block to dump prototype in. See GlobalState.
        self.proto = proto
        self.impl = impl
        self.init = init
        self.cleanup = cleanup
        self.requires = requires
        self._cache = {}
        self.specialize_list = []
        self.proto_block = proto_block

    def get_tree(self):
        pass

    def specialize(self, pyrex_type=None, **data):
        # Dicts aren't hashable...
        if pyrex_type is not None:
            data['type'] = pyrex_type.declaration_code('')
            data['type_name'] = pyrex_type.specialization_name()
        key = data.items(); key.sort(); key = tuple(key)
        try:
            return self._cache[key]
        except KeyError:
            if self.requires is None:
                requires = None
            else:
                requires = [r.specialize(data) for r in self.requires]
            s = self._cache[key] = UtilityCode(
                                        none_or_sub(self.proto, data),
                                        none_or_sub(self.impl, data),
                                        none_or_sub(self.init, data),
                                        none_or_sub(self.cleanup, data),
                                        requires, self.proto_block)
            self.specialize_list.append(s)
            return s

    def put_code(self, output):
        if self.requires:
            for dependency in self.requires:
                output.use_utility_code(dependency)
        if self.proto:
            output[self.proto_block].put(self.proto)
        if self.impl:
            output['utility_code_def'].put(self.impl)
        if self.init:
            writer = output['init_globals']
            if isinstance(self.init, basestring):
                writer.put(self.init)
            else:
                self.init(writer, output.module_pos)
        if self.cleanup and Options.generate_cleanup_code:
            writer = output['cleanup_globals']
            if isinstance(self.cleanup, basestring):
                writer.put(self.cleanup)
            else:
                self.cleanup(writer, output.module_pos)



class FunctionState(object):
    # return_label     string          function return point label
    # error_label      string          error catch point label
    # continue_label   string          loop continue point label
    # break_label      string          loop break point label
    # return_from_error_cleanup_label string
    # label_counter    integer         counter for naming labels
    # in_try_finally   boolean         inside try of try...finally
    # exc_vars         (string * 3)    exception variables for reraise, or None

    # Not used for now, perhaps later
    def __init__(self, owner, names_taken=set()):
        self.names_taken = names_taken
        self.owner = owner

        self.error_label = None
        self.label_counter = 0
        self.labels_used = set()
        self.return_label = self.new_label()
        self.new_error_label()
        self.continue_label = None
        self.break_label = None

        self.in_try_finally = 0
        self.exc_vars = None

        self.temps_allocated = [] # of (name, type, manage_ref)
        self.temps_free = {} # (type, manage_ref) -> list of free vars with same type/managed status
        self.temps_used_type = {} # name -> (type, manage_ref)
        self.temp_counter = 0
        self.closure_temps = None

        # This is used to collect temporaries, useful to find out which temps
        # need to be privatized in parallel sections
        self.collect_temps_stack = []

        # This is used for the error indicator, which needs to be local to the
        # function. It used to be global, which relies on the GIL being held.
        # However, exceptions may need to be propagated through 'nogil'
        # sections, in which case we introduce a race condition.
        self.should_declare_error_indicator = False

    # labels

    def new_label(self, name=None):
        n = self.label_counter
        self.label_counter = n + 1
        label = "%s%d" % (Naming.label_prefix, n)
        if name is not None:
            label += '_' + name
        return label

    def new_error_label(self):
        old_err_lbl = self.error_label
        self.error_label = self.new_label('error')
        return old_err_lbl

    def get_loop_labels(self):
        return (
            self.continue_label,
            self.break_label)

    def set_loop_labels(self, labels):
        (self.continue_label,
         self.break_label) = labels

    def new_loop_labels(self):
        old_labels = self.get_loop_labels()
        self.set_loop_labels(
            (self.new_label("continue"),
             self.new_label("break")))
        return old_labels

    def get_all_labels(self):
        return (
            self.continue_label,
            self.break_label,
            self.return_label,
            self.error_label)

    def set_all_labels(self, labels):
        (self.continue_label,
         self.break_label,
         self.return_label,
         self.error_label) = labels

    def all_new_labels(self):
        old_labels = self.get_all_labels()
        new_labels = []
        for old_label in old_labels:
            if old_label:
                new_labels.append(self.new_label())
            else:
                new_labels.append(old_label)
        self.set_all_labels(new_labels)
        return old_labels

    def use_label(self, lbl):
        self.labels_used.add(lbl)

    def label_used(self, lbl):
        return lbl in self.labels_used

    # temp handling

    def allocate_temp(self, type, manage_ref):
        """
        Allocates a temporary (which may create a new one or get a previously
        allocated and released one of the same type). Type is simply registered
        and handed back, but will usually be a PyrexType.

        If type.is_pyobject, manage_ref comes into play. If manage_ref is set to
        True, the temp will be decref-ed on return statements and in exception
        handling clauses. Otherwise the caller has to deal with any reference
        counting of the variable.

        If not type.is_pyobject, then manage_ref will be ignored, but it
        still has to be passed. It is recommended to pass False by convention
        if it is known that type will never be a Python object.

        A C string referring to the variable is returned.
        """
        if not type.is_pyobject:
            # Make manage_ref canonical, so that manage_ref will always mean
            # a decref is needed.
            manage_ref = False
        freelist = self.temps_free.get((type, manage_ref))
        if freelist is not None and len(freelist) > 0:
            result = freelist.pop()
        else:
            while True:
                self.temp_counter += 1
                result = "%s%d" % (Naming.codewriter_temp_prefix, self.temp_counter)
                if not result in self.names_taken: break
            self.temps_allocated.append((result, type, manage_ref))
        self.temps_used_type[result] = (type, manage_ref)
        if DebugFlags.debug_temp_code_comments:
            self.owner.putln("/* %s allocated */" % result)

        if self.collect_temps_stack:
            self.collect_temps_stack[-1].add((result, type))

        return result

    def release_temp(self, name):
        """
        Releases a temporary so that it can be reused by other code needing
        a temp of the same type.
        """
        type, manage_ref = self.temps_used_type[name]
        freelist = self.temps_free.get((type, manage_ref))
        if freelist is None:
            freelist = []
            self.temps_free[(type, manage_ref)] = freelist
        if name in freelist:
            raise RuntimeError("Temp %s freed twice!" % name)
        freelist.append(name)
        if DebugFlags.debug_temp_code_comments:
            self.owner.putln("/* %s released */" % name)

    def temps_in_use(self):
        """Return a list of (cname,type,manage_ref) tuples of temp names and their type
        that are currently in use.
        """
        used = []
        for name, type, manage_ref in self.temps_allocated:
            freelist = self.temps_free.get((type, manage_ref))
            if freelist is None or name not in freelist:
                used.append((name, type, manage_ref))
        return used

    def temps_holding_reference(self):
        """Return a list of (cname,type) tuples of temp names and their type
        that are currently in use. This includes only temps of a
        Python object type which owns its reference.
        """
        return [(name, type)
                for name, type, manage_ref in self.temps_in_use()
                if manage_ref]

    def all_managed_temps(self):
        """Return a list of (cname, type) tuples of refcount-managed Python objects.
        """
        return [(cname, type)
                for cname, type, manage_ref in self.temps_allocated
                if manage_ref]

    def all_free_managed_temps(self):
        """Return a list of (cname, type) tuples of refcount-managed Python
        objects that are not currently in use.  This is used by
        try-except and try-finally blocks to clean up temps in the
        error case.
        """
        return [(cname, type)
                for (type, manage_ref), freelist in self.temps_free.items()
                if manage_ref
                for cname in freelist]

    def start_collecting_temps(self):
        """
        Useful to find out which temps were used in a code block
        """
        self.collect_temps_stack.append(set())

    def stop_collecting_temps(self):
        return self.collect_temps_stack.pop()

    def init_closure_temps(self, scope):
        self.closure_temps = ClosureTempAllocator(scope)


class IntConst(object):
    """Global info about a Python integer constant held by GlobalState.
    """
    # cname     string
    # value     int
    # is_long   boolean

    def __init__(self, cname, value, is_long):
        self.cname = cname
        self.value = value
        self.is_long = is_long

class PyObjectConst(object):
    """Global info about a generic constant held by GlobalState.
    """
    # cname       string
    # type        PyrexType

    def __init__(self, cname, type):
        self.cname = cname
        self.type = type

cython.declare(possible_unicode_identifier=object, possible_bytes_identifier=object,
               nice_identifier=object, find_alphanums=object)
possible_unicode_identifier = re.compile(ur"(?![0-9])\w+$", re.U).match
possible_bytes_identifier = re.compile(r"(?![0-9])\w+$".encode('ASCII')).match
nice_identifier = re.compile(r'\A[a-zA-Z0-9_]+\Z').match
find_alphanums = re.compile('([a-zA-Z0-9]+)').findall

class StringConst(object):
    """Global info about a C string constant held by GlobalState.
    """
    # cname            string
    # text             EncodedString or BytesLiteral
    # py_strings       {(identifier, encoding) : PyStringConst}

    def __init__(self, cname, text, byte_string):
        self.cname = cname
        self.text = text
        self.escaped_value = StringEncoding.escape_byte_string(byte_string)
        self.py_strings = None
        self.py_versions = []

    def add_py_version(self, version):
        if not version:
            self.py_versions = [2,3]
        elif version not in self.py_versions:
            self.py_versions.append(version)

    def get_py_string_const(self, encoding, identifier=None,
                            is_str=False, py3str_cstring=None):
        py_strings = self.py_strings
        text = self.text

        is_str = bool(identifier or is_str)
        is_unicode = encoding is None and not is_str

        if encoding is None:
            # unicode string
            encoding_key = None
        else:
            # bytes or str
            encoding = encoding.lower()
            if encoding in ('utf8', 'utf-8', 'ascii', 'usascii', 'us-ascii'):
                encoding = None
                encoding_key = None
            else:
                encoding_key = ''.join(find_alphanums(encoding))

        key = (is_str, is_unicode, encoding_key, py3str_cstring)
        if py_strings is not None:
            try:
                return py_strings[key]
            except KeyError:
                pass
        else:
            self.py_strings = {}

        if identifier:
            intern = True
        elif identifier is None:
            if isinstance(text, unicode):
                intern = bool(possible_unicode_identifier(text))
            else:
                intern = bool(possible_bytes_identifier(text))
        else:
            intern = False
        if intern:
            prefix = Naming.interned_str_prefix
        else:
            prefix = Naming.py_const_prefix
        pystring_cname = "%s%s_%s" % (
            prefix,
            (is_str and 's') or (is_unicode and 'u') or 'b',
            self.cname[len(Naming.const_prefix):])

        py_string = PyStringConst(
            pystring_cname, encoding, is_unicode, is_str, py3str_cstring, intern)
        self.py_strings[key] = py_string
        return py_string

class PyStringConst(object):
    """Global info about a Python string constant held by GlobalState.
    """
    # cname       string
    # py3str_cstring string
    # encoding    string
    # intern      boolean
    # is_unicode  boolean
    # is_str      boolean

    def __init__(self, cname, encoding, is_unicode, is_str=False,
                 py3str_cstring=None, intern=False):
        self.cname = cname
        self.py3str_cstring = py3str_cstring
        self.encoding = encoding
        self.is_str = is_str
        self.is_unicode = is_unicode
        self.intern = intern

    def __lt__(self, other):
        return self.cname < other.cname


class GlobalState(object):
    # filename_table   {string : int}  for finding filename table indexes
    # filename_list    [string]        filenames in filename table order
    # input_file_contents dict         contents (=list of lines) of any file that was used as input
    #                                  to create this output C code.  This is
    #                                  used to annotate the comments.
    #
    # utility_codes   set                IDs of used utility code (to avoid reinsertion)
    #
    # declared_cnames  {string:Entry}  used in a transition phase to merge pxd-declared
    #                                  constants etc. into the pyx-declared ones (i.e,
    #                                  check if constants are already added).
    #                                  In time, hopefully the literals etc. will be
    #                                  supplied directly instead.
    #
    # const_cname_counter int          global counter for constant identifiers
    #

    # parts            {string:CCodeWriter}


    # interned_strings
    # consts
    # interned_nums

    # directives       set             Temporary variable used to track
    #                                  the current set of directives in the code generation
    #                                  process.

    directives = {}

    code_layout = [
        'h_code',
        'filename_table',
        'utility_code_proto_before_types',
        'numeric_typedefs',          # Let these detailed individual parts stay!,
        'complex_type_declarations', # as the proper solution is to make a full DAG...
        'type_declarations',         # More coarse-grained blocks would simply hide
        'utility_code_proto',        # the ugliness, not fix it
        'module_declarations',
        'typeinfo',
        'before_global_var',
        'global_var',
        'decls',
        'all_the_rest',
        'pystring_table',
        'cached_builtins',
        'cached_constants',
        'init_globals',
        'init_module',
        'cleanup_globals',
        'cleanup_module',
        'main_method',
        'utility_code_def',
        'end'
    ]


    def __init__(self, writer, emit_linenums=False):
        self.filename_table = {}
        self.filename_list = []
        self.input_file_contents = {}
        self.utility_codes = set()
        self.declared_cnames = {}
        self.in_utility_code_generation = False
        self.emit_linenums = emit_linenums
        self.parts = {}

        self.const_cname_counter = 1
        self.string_const_index = {}
        self.int_const_index = {}
        self.py_constants = []

        assert writer.globalstate is None
        writer.globalstate = self
        self.rootwriter = writer

    def initialize_main_c_code(self):
        rootwriter = self.rootwriter
        for part in self.code_layout:
            self.parts[part] = rootwriter.insertion_point()

        if not Options.cache_builtins:
            del self.parts['cached_builtins']
        else:
            w = self.parts['cached_builtins']
            w.enter_cfunc_scope()
            w.putln("static int __Pyx_InitCachedBuiltins(void) {")

        w = self.parts['cached_constants']
        w.enter_cfunc_scope()
        w.putln("")
        w.putln("static int __Pyx_InitCachedConstants(void) {")
        w.put_declare_refcount_context()
        w.put_setup_refcount_context("__Pyx_InitCachedConstants")

        w = self.parts['init_globals']
        w.enter_cfunc_scope()
        w.putln("")
        w.putln("static int __Pyx_InitGlobals(void) {")

        if not Options.generate_cleanup_code:
            del self.parts['cleanup_globals']
        else:
            w = self.parts['cleanup_globals']
            w.enter_cfunc_scope()
            w.putln("")
            w.putln("static void __Pyx_CleanupGlobals(void) {")

        #
        # utility_code_def
        #
        code = self.parts['utility_code_def']
        if self.emit_linenums:
            code.write('\n#line 1 "cython_utility"\n')
        code.putln("")
        code.putln("/* Runtime support code */")

    def finalize_main_c_code(self):
        self.close_global_decls()

        #
        # utility_code_def
        #
        code = self.parts['utility_code_def']
        import PyrexTypes
        code.put(PyrexTypes.type_conversion_functions)
        code.putln("")

    def __getitem__(self, key):
        return self.parts[key]

    #
    # Global constants, interned objects, etc.
    #
    def close_global_decls(self):
        # This is called when it is known that no more global declarations will
        # declared.
        self.generate_const_declarations()
        if Options.cache_builtins:
            w = self.parts['cached_builtins']
            w.putln("return 0;")
            if w.label_used(w.error_label):
                w.put_label(w.error_label)
                w.putln("return -1;")
            w.putln("}")
            w.exit_cfunc_scope()

        w = self.parts['cached_constants']
        w.put_finish_refcount_context()
        w.putln("return 0;")
        if w.label_used(w.error_label):
            w.put_label(w.error_label)
            w.put_finish_refcount_context()
            w.putln("return -1;")
        w.putln("}")
        w.exit_cfunc_scope()

        w = self.parts['init_globals']
        w.putln("return 0;")
        if w.label_used(w.error_label):
            w.put_label(w.error_label)
            w.putln("return -1;")
        w.putln("}")
        w.exit_cfunc_scope()

        if Options.generate_cleanup_code:
            w = self.parts['cleanup_globals']
            w.putln("}")
            w.exit_cfunc_scope()

        if Options.generate_cleanup_code:
            w = self.parts['cleanup_module']
            w.putln("}")
            w.exit_cfunc_scope()

    def put_pyobject_decl(self, entry):
        self['global_var'].putln("static PyObject *%s;" % entry.cname)

    # constant handling at code generation time

    def get_cached_constants_writer(self):
        return self.parts['cached_constants']

    def get_int_const(self, str_value, longness=False):
        longness = bool(longness)
        try:
            c = self.int_const_index[(str_value, longness)]
        except KeyError:
            c = self.new_int_const(str_value, longness)
        return c

    def get_py_const(self, type, prefix='', cleanup_level=None):
        # create a new Python object constant
        const = self.new_py_const(type, prefix)
        if cleanup_level is not None \
               and cleanup_level <= Options.generate_cleanup_code:
            cleanup_writer = self.parts['cleanup_globals']
            cleanup_writer.put_xdecref_clear(const.cname, type, nanny=False)
        return const

    def get_string_const(self, text, py_version=None):
        # return a C string constant, creating a new one if necessary
        if text.is_unicode:
            byte_string = text.utf8encode()
        else:
            byte_string = text.byteencode()
        try:
            c = self.string_const_index[byte_string]
        except KeyError:
            c = self.new_string_const(text, byte_string)
        c.add_py_version(py_version)
        return c

    def get_py_string_const(self, text, identifier=None,
                            is_str=False, unicode_value=None):
        # return a Python string constant, creating a new one if necessary
        py3str_cstring = None
        if is_str and unicode_value is not None \
               and unicode_value.utf8encode() != text.byteencode():
            py3str_cstring = self.get_string_const(unicode_value, py_version=3)
            c_string = self.get_string_const(text, py_version=2)
        else:
            c_string = self.get_string_const(text)
        py_string = c_string.get_py_string_const(
            text.encoding, identifier, is_str, py3str_cstring)
        return py_string

    def get_interned_identifier(self, text):
        return self.get_py_string_const(text, identifier=True)

    def new_string_const(self, text, byte_string):
        cname = self.new_string_const_cname(byte_string)
        c = StringConst(cname, text, byte_string)
        self.string_const_index[byte_string] = c
        return c

    def new_int_const(self, value, longness):
        cname = self.new_int_const_cname(value, longness)
        c = IntConst(cname, value, longness)
        self.int_const_index[(value, longness)] = c
        return c

    def new_py_const(self, type, prefix=''):
        cname = self.new_const_cname(prefix)
        c = PyObjectConst(cname, type)
        self.py_constants.append(c)
        return c

    def new_string_const_cname(self, bytes_value, intern=None):
        # Create a new globally-unique nice name for a C string constant.
        try:
            value = bytes_value.decode('ASCII')
        except UnicodeError:
            return self.new_const_cname()

        if len(value) < 20 and nice_identifier(value):
            return "%s_%s" % (Naming.const_prefix, value)
        else:
            return self.new_const_cname()

    def new_int_const_cname(self, value, longness):
        if longness:
            value += 'L'
        cname = "%s%s" % (Naming.interned_num_prefix, value)
        cname = cname.replace('-', 'neg_').replace('.','_')
        return cname

    def new_const_cname(self, prefix=''):
        n = self.const_cname_counter
        self.const_cname_counter += 1
        return "%s%s%d" % (Naming.const_prefix, prefix, n)

    def add_cached_builtin_decl(self, entry):
        if entry.is_builtin and entry.is_const:
            if self.should_declare(entry.cname, entry):
                self.put_pyobject_decl(entry)
                w = self.parts['cached_builtins']
                condition = None
                if entry.name in non_portable_builtins_map:
                    condition, replacement = non_portable_builtins_map[entry.name]
                    w.putln('#if %s' % condition)
                    self.put_cached_builtin_init(
                        entry.pos, StringEncoding.EncodedString(replacement),
                        entry.cname)
                    w.putln('#else')
                self.put_cached_builtin_init(
                    entry.pos, StringEncoding.EncodedString(entry.name),
                    entry.cname)
                if condition:
                    w.putln('#endif')

    def put_cached_builtin_init(self, pos, name, cname):
        w = self.parts['cached_builtins']
        interned_cname = self.get_interned_identifier(name).cname
        from ExprNodes import get_name_interned_utility_code
        self.use_utility_code(get_name_interned_utility_code)
        w.putln('%s = __Pyx_GetName(%s, %s); if (!%s) %s' % (
            cname,
            Naming.builtins_cname,
            interned_cname,
            cname,
            w.error_goto(pos)))

    def generate_const_declarations(self):
        self.generate_string_constants()
        self.generate_int_constants()
        self.generate_object_constant_decls()

    def generate_object_constant_decls(self):
        consts = [ (len(c.cname), c.cname, c)
                   for c in self.py_constants ]
        consts.sort()
        decls_writer = self.parts['decls']
        for _, cname, c in consts:
            decls_writer.putln(
                "static %s;" % c.type.declaration_code(cname))

    def generate_string_constants(self):
        c_consts = [ (len(c.cname), c.cname, c)
                     for c in self.string_const_index.values() ]
        c_consts.sort()
        py_strings = []

        decls_writer = self.parts['decls']
        for _, cname, c in c_consts:
            conditional = False
            if c.py_versions and (2 not in c.py_versions or 3 not in c.py_versions):
                conditional = True
                decls_writer.putln("#if PY_MAJOR_VERSION %s 3" % (
                    (2 in c.py_versions) and '<' or '>='))
            decls_writer.putln('static char %s[] = "%s";' % (
                cname, StringEncoding.split_string_literal(c.escaped_value)))
            if conditional:
                decls_writer.putln("#endif")
            if c.py_strings is not None:
                for py_string in c.py_strings.values():
                    py_strings.append((c.cname, len(py_string.cname), py_string))

        if py_strings:
            import Nodes
            self.use_utility_code(Nodes.init_string_tab_utility_code)

            py_strings.sort()
            w = self.parts['pystring_table']
            w.putln("")
            w.putln("static __Pyx_StringTabEntry %s[] = {" %
                                      Naming.stringtab_cname)
            for c_cname, _, py_string in py_strings:
                if not py_string.is_str or not py_string.encoding or \
                       py_string.encoding in ('ASCII', 'USASCII', 'US-ASCII',
                                              'UTF8', 'UTF-8'):
                    encoding = '0'
                else:
                    encoding = '"%s"' % py_string.encoding.lower()

                decls_writer.putln(
                    "static PyObject *%s;" % py_string.cname)
                if py_string.py3str_cstring:
                    w.putln("#if PY_MAJOR_VERSION >= 3")
                    w.putln(
                        "{&%s, %s, sizeof(%s), %s, %d, %d, %d}," % (
                        py_string.cname,
                        py_string.py3str_cstring.cname,
                        py_string.py3str_cstring.cname,
                        '0', 1, 0,
                        py_string.intern
                        ))
                    w.putln("#else")
                w.putln(
                    "{&%s, %s, sizeof(%s), %s, %d, %d, %d}," % (
                    py_string.cname,
                    c_cname,
                    c_cname,
                    encoding,
                    py_string.is_unicode,
                    py_string.is_str,
                    py_string.intern
                    ))
                if py_string.py3str_cstring:
                    w.putln("#endif")
            w.putln("{0, 0, 0, 0, 0, 0, 0}")
            w.putln("};")

            init_globals = self.parts['init_globals']
            init_globals.putln(
                "if (__Pyx_InitStrings(%s) < 0) %s;" % (
                    Naming.stringtab_cname,
                    init_globals.error_goto(self.module_pos)))

    def generate_int_constants(self):
        consts = [ (len(c.value), c.value, c.is_long, c)
                   for c in self.int_const_index.values() ]
        consts.sort()
        decls_writer = self.parts['decls']
        for _, value, longness, c in consts:
            cname = c.cname
            decls_writer.putln("static PyObject *%s;" % cname)
            if longness:
                function = '%s = PyLong_FromString((char *)"%s", 0, 0); %s;'
            elif Utils.long_literal(value):
                function = '%s = PyInt_FromString((char *)"%s", 0, 0); %s;'
            else:
                function = "%s = PyInt_FromLong(%s); %s;"
            init_globals = self.parts['init_globals']
            init_globals.putln(function % (
                cname,
                value,
                init_globals.error_goto_if_null(cname, self.module_pos)))

    # The functions below are there in a transition phase only
    # and will be deprecated. They are called from Nodes.BlockNode.
    # The copy&paste duplication is intentional in order to be able
    # to see quickly how BlockNode worked, until this is replaced.

    def should_declare(self, cname, entry):
        if cname in self.declared_cnames:
            other = self.declared_cnames[cname]
            assert str(entry.type) == str(other.type)
            assert entry.init == other.init
            return False
        else:
            self.declared_cnames[cname] = entry
            return True

    #
    # File name state
    #

    def lookup_filename(self, filename):
        try:
            index = self.filename_table[filename]
        except KeyError:
            index = len(self.filename_list)
            self.filename_list.append(filename)
            self.filename_table[filename] = index
        return index

    def commented_file_contents(self, source_desc):
        try:
            return self.input_file_contents[source_desc]
        except KeyError:
            pass
        source_file = source_desc.get_lines(encoding='ASCII',
                                            error_handling='ignore')
        try:
            F = [u' * ' + line.rstrip().replace(
                    u'*/', u'*[inserted by cython to avoid comment closer]/'
                    ).replace(
                    u'/*', u'/[inserted by cython to avoid comment start]*'
                    )
                 for line in source_file]
        finally:
            if hasattr(source_file, 'close'):
                source_file.close()
        if not F: F.append(u'')
        self.input_file_contents[source_desc] = F
        return F

    #
    # Utility code state
    #

    def use_utility_code(self, utility_code):
        """
        Adds code to the C file. utility_code should
        a) implement __eq__/__hash__ for the purpose of knowing whether the same
           code has already been included
        b) implement put_code, which takes a globalstate instance

        See UtilityCode.
        """
        if utility_code not in self.utility_codes:
            self.utility_codes.add(utility_code)
            utility_code.put_code(self)


def funccontext_property(name):
    try:
        import operator
        attribute_of = operator.attrgetter(name)
    except:
        def attribute_of(o):
            return getattr(o, name)

    def get(self):
        return attribute_of(self.funcstate)
    def set(self, value):
        setattr(self.funcstate, name, value)
    return property(get, set)


class CCodeWriter(object):
    """
    Utility class to output C code.

    When creating an insertion point one must care about the state that is
    kept:
    - formatting state (level, bol) is cloned and used in insertion points
      as well
    - labels, temps, exc_vars: One must construct a scope in which these can
      exist by calling enter_cfunc_scope/exit_cfunc_scope (these are for
      sanity checking and forward compatabilty). Created insertion points
      looses this scope and cannot access it.
    - marker: Not copied to insertion point
    - filename_table, filename_list, input_file_contents: All codewriters
      coming from the same root share the same instances simultaneously.
    """

    # f                   file            output file
    # buffer              StringIOTree

    # level               int             indentation level
    # bol                 bool            beginning of line?
    # marker              string          comment to emit before next line
    # funcstate           FunctionState   contains state local to a C function used for code
    #                                     generation (labels and temps state etc.)
    # globalstate         GlobalState     contains state global for a C file (input file info,
    #                                     utility code, declared constants etc.)
    # emit_linenums       boolean         whether or not to write #line pragmas
    #
    # c_line_in_traceback boolean         append the c file and line number to the traceback for exceptions
    #
    # pyclass_stack       list            used during recursive code generation to pass information
    #                                     about the current class one is in

    globalstate = None

    def __init__(self, create_from=None, buffer=None, copy_formatting=False, emit_linenums=None, c_line_in_traceback=True):
        if buffer is None: buffer = StringIOTree()
        self.buffer = buffer
        self.marker = None
        self.last_marker_line = 0
        self.source_desc = ""
        self.pyclass_stack = []

        self.funcstate = None
        self.level = 0
        self.call_level = 0
        self.bol = 1

        if create_from is not None:
            # Use same global state
            self.globalstate = create_from.globalstate
            # Clone formatting state
            if copy_formatting:
                self.level = create_from.level
                self.bol = create_from.bol
                self.call_level = create_from.call_level
        if emit_linenums is None and self.globalstate:
            self.emit_linenums = self.globalstate.emit_linenums
        else:
            self.emit_linenums = emit_linenums
        self.c_line_in_traceback = c_line_in_traceback

    def create_new(self, create_from, buffer, copy_formatting):
        # polymorphic constructor -- very slightly more versatile
        # than using __class__
        result = CCodeWriter(create_from, buffer, copy_formatting, c_line_in_traceback=self.c_line_in_traceback)
        return result

    def copyto(self, f):
        self.buffer.copyto(f)

    def getvalue(self):
        return self.buffer.getvalue()

    def write(self, s):
        # also put invalid markers (lineno 0), to indicate that those lines
        # have no Cython source code correspondence
        if self.marker is None:
            cython_lineno = self.last_marker_line
        else:
            cython_lineno = self.marker[0]

        self.buffer.markers.extend([cython_lineno] * s.count('\n'))
        self.buffer.write(s)

    def insertion_point(self):
        other = self.create_new(create_from=self, buffer=self.buffer.insertion_point(), copy_formatting=True)
        return other

    def new_writer(self):
        """
        Creates a new CCodeWriter connected to the same global state, which
        can later be inserted using insert.
        """
        return CCodeWriter(create_from=self, c_line_in_traceback=self.c_line_in_traceback)

    def insert(self, writer):
        """
        Inserts the contents of another code writer (created with
        the same global state) in the current location.

        It is ok to write to the inserted writer also after insertion.
        """
        assert writer.globalstate is self.globalstate
        self.buffer.insert(writer.buffer)

    # Properties delegated to function scope
    label_counter = funccontext_property("label_counter")
    return_label = funccontext_property("return_label")
    error_label = funccontext_property("error_label")
    labels_used = funccontext_property("labels_used")
    continue_label = funccontext_property("continue_label")
    break_label = funccontext_property("break_label")
    return_from_error_cleanup_label = funccontext_property("return_from_error_cleanup_label")

    # Functions delegated to function scope
    def new_label(self, name=None):    return self.funcstate.new_label(name)
    def new_error_label(self):         return self.funcstate.new_error_label()
    def get_loop_labels(self):         return self.funcstate.get_loop_labels()
    def set_loop_labels(self, labels): return self.funcstate.set_loop_labels(labels)
    def new_loop_labels(self):         return self.funcstate.new_loop_labels()
    def get_all_labels(self):          return self.funcstate.get_all_labels()
    def set_all_labels(self, labels):  return self.funcstate.set_all_labels(labels)
    def all_new_labels(self):          return self.funcstate.all_new_labels()
    def use_label(self, lbl):          return self.funcstate.use_label(lbl)
    def label_used(self, lbl):         return self.funcstate.label_used(lbl)


    def enter_cfunc_scope(self):
        self.funcstate = FunctionState(self)

    def exit_cfunc_scope(self):
        self.funcstate = None

    # constant handling

    def get_py_num(self, str_value, longness):
        return self.globalstate.get_int_const(str_value, longness).cname

    def get_py_const(self, type, prefix='', cleanup_level=None):
        return self.globalstate.get_py_const(type, prefix, cleanup_level).cname

    def get_string_const(self, text):
        return self.globalstate.get_string_const(text).cname

    def get_py_string_const(self, text, identifier=None,
                            is_str=False, unicode_value=None):
        return self.globalstate.get_py_string_const(
            text, identifier, is_str, unicode_value).cname

    def get_argument_default_const(self, type):
        return self.globalstate.get_py_const(type).cname

    def intern(self, text):
        return self.get_py_string_const(text)

    def intern_identifier(self, text):
        return self.get_py_string_const(text, identifier=True)

    def get_cached_constants_writer(self):
        return self.globalstate.get_cached_constants_writer()

    # code generation

    def putln(self, code = "", safe=False):
        if self.marker and self.bol:
            self.emit_marker()
        if self.emit_linenums and self.last_marker_line != 0:
            self.write('\n#line %s "%s"\n' % (self.last_marker_line, self.source_desc))

        if code:
            if safe:
                self.put_safe(code)
            else:
                self.put(code)
        self.write("\n");
        self.bol = 1

    def emit_marker(self):
        self.write("\n");
        self.indent()
        self.write("/* %s */\n" % self.marker[1])
        self.last_marker_line = self.marker[0]
        self.marker = None

    def put_safe(self, code):
        # put code, but ignore {}
        self.write(code)
        self.bol = 0

    def put(self, code):
        fix_indent = False
        if "{" in code:
            dl = code.count("{")
        else:
            dl = 0
        if "}" in code:
            dl -= code.count("}")
            if dl < 0:
                self.level += dl
            elif dl == 0 and code[0] == "}":
                # special cases like "} else {" need a temporary dedent
                fix_indent = True
                self.level -= 1
        if self.bol:
            self.indent()
        self.write(code)
        self.bol = 0
        if dl > 0:
            self.level += dl
        elif fix_indent:
            self.level += 1

    def increase_indent(self):
        self.level = self.level + 1

    def decrease_indent(self):
        self.level = self.level - 1

    def begin_block(self):
        self.putln("{")
        self.increase_indent()

    def end_block(self):
        self.decrease_indent()
        self.putln("}")

    def indent(self):
        self.write("  " * self.level)

    def get_py_version_hex(self, pyversion):
        return "0x%02X%02X%02X%02X" % (tuple(pyversion) + (0,0,0,0))[:4]

    def mark_pos(self, pos):
        if pos is None:
            return
        source_desc, line, col = pos
        if self.last_marker_line == line:
            return
        assert isinstance(source_desc, SourceDescriptor)
        contents = self.globalstate.commented_file_contents(source_desc)
        lines = contents[max(0,line-3):line] # line numbers start at 1
        lines[-1] += u'             # <<<<<<<<<<<<<<'
        lines += contents[line:line+2]

        marker = u'"%s":%d\n%s\n' % (
            source_desc.get_escaped_description(), line, u'\n'.join(lines))
        self.marker = (line, marker)
        if self.emit_linenums:
            self.source_desc = source_desc.get_escaped_description()

    def put_label(self, lbl):
        if lbl in self.funcstate.labels_used:
            self.putln("%s:;" % lbl)

    def put_goto(self, lbl):
        self.funcstate.use_label(lbl)
        self.putln("goto %s;" % lbl)

    def put_var_declaration(self, entry, storage_class="",
                            dll_linkage = None, definition = True):
        #print "Code.put_var_declaration:", entry.name, "definition =", definition ###
        if entry.visibility == 'private' and not (definition or entry.defined_in_pxd):
            #print "...private and not definition, skipping", entry.cname ###
            return
        if entry.visibility == "private" and not entry.used:
            #print "...private and not used, skipping", entry.cname ###
            return
        if storage_class:
            self.put("%s " % storage_class)
        if not entry.cf_used:
            self.put('CYTHON_UNUSED ')
        self.put(entry.type.declaration_code(
                entry.cname, dll_linkage = dll_linkage))
        if entry.init is not None:
            self.put_safe(" = %s" % entry.type.literal_code(entry.init))
        elif entry.type.is_pyobject:
            self.put(" = NULL");
        self.putln(";")

    def put_temp_declarations(self, func_context):
        for name, type, manage_ref in func_context.temps_allocated:
            decl = type.declaration_code(name)
            if type.is_pyobject:
                self.putln("%s = NULL;" % decl)
            else:
                self.putln("%s;" % decl)

    def put_h_guard(self, guard):
        self.putln("#ifndef %s" % guard)
        self.putln("#define %s" % guard)

    def unlikely(self, cond):
        if Options.gcc_branch_hints:
            return 'unlikely(%s)' % cond
        else:
            return cond

    # Python objects and reference counting

    def entry_as_pyobject(self, entry):
        type = entry.type
        if (not entry.is_self_arg and not entry.type.is_complete()
            or entry.type.is_extension_type):
            return "(PyObject *)" + entry.cname
        else:
            return entry.cname

    def as_pyobject(self, cname, type):
        from PyrexTypes import py_object_type, typecast
        return typecast(py_object_type, type, cname)

    def put_gotref(self, cname):
        self.putln("__Pyx_GOTREF(%s);" % cname)

    def put_giveref(self, cname):
        self.putln("__Pyx_GIVEREF(%s);" % cname)

    def put_xgiveref(self, cname):
        self.putln("__Pyx_XGIVEREF(%s);" % cname)

    def put_xgotref(self, cname):
        self.putln("__Pyx_XGOTREF(%s);" % cname)

    def put_incref(self, cname, type, nanny=True):
        if nanny:
            self.putln("__Pyx_INCREF(%s);" % self.as_pyobject(cname, type))
        else:
            self.putln("Py_INCREF(%s);" % self.as_pyobject(cname, type))

    def put_decref(self, cname, type, nanny=True):
        if nanny:
            self.putln("__Pyx_DECREF(%s);" % self.as_pyobject(cname, type))
        else:
            self.putln("Py_DECREF(%s);" % self.as_pyobject(cname, type))

    def put_var_gotref(self, entry):
        if entry.type.is_pyobject:
            self.putln("__Pyx_GOTREF(%s);" % self.entry_as_pyobject(entry))

    def put_var_giveref(self, entry):
        if entry.type.is_pyobject:
            self.putln("__Pyx_GIVEREF(%s);" % self.entry_as_pyobject(entry))

    def put_var_xgotref(self, entry):
        if entry.type.is_pyobject:
            self.putln("__Pyx_XGOTREF(%s);" % self.entry_as_pyobject(entry))

    def put_var_xgiveref(self, entry):
        if entry.type.is_pyobject:
            self.putln("__Pyx_XGIVEREF(%s);" % self.entry_as_pyobject(entry))

    def put_var_incref(self, entry):
        if entry.type.is_pyobject:
            self.putln("__Pyx_INCREF(%s);" % self.entry_as_pyobject(entry))

    def put_decref_clear(self, cname, type, nanny=True):
        from PyrexTypes import py_object_type, typecast
        if nanny:
            self.putln("__Pyx_DECREF(%s); %s = 0;" % (
                typecast(py_object_type, type, cname), cname))
        else:
            self.putln("Py_DECREF(%s); %s = 0;" % (
                typecast(py_object_type, type, cname), cname))

    def put_xdecref(self, cname, type, nanny=True):
        if nanny:
            self.putln("__Pyx_XDECREF(%s);" % self.as_pyobject(cname, type))
        else:
            self.putln("Py_XDECREF(%s);" % self.as_pyobject(cname, type))

    def put_xdecref_clear(self, cname, type, nanny=True):
        if nanny:
            self.putln("__Pyx_XDECREF(%s); %s = 0;" % (
                self.as_pyobject(cname, type), cname))
        else:
            self.putln("Py_XDECREF(%s); %s = 0;" % (
                self.as_pyobject(cname, type), cname))

    def put_var_decref(self, entry):
        if entry.type.is_pyobject:
            self.putln("__Pyx_XDECREF(%s);" % self.entry_as_pyobject(entry))

    def put_var_decref_clear(self, entry):
        if entry.type.is_pyobject:
            self.putln("__Pyx_DECREF(%s); %s = 0;" % (
                self.entry_as_pyobject(entry), entry.cname))

    def put_var_xdecref(self, entry):
        if entry.type.is_pyobject:
            self.putln("__Pyx_XDECREF(%s);" % self.entry_as_pyobject(entry))

    def put_var_xdecref_clear(self, entry):
        if entry.type.is_pyobject:
            self.putln("__Pyx_XDECREF(%s); %s = 0;" % (
                self.entry_as_pyobject(entry), entry.cname))

    def put_var_decrefs(self, entries, used_only = 0):
        for entry in entries:
            if not used_only or entry.used:
                if entry.xdecref_cleanup:
                    self.put_var_xdecref(entry)
                else:
                    self.put_var_decref(entry)

    def put_var_xdecrefs(self, entries):
        for entry in entries:
            self.put_var_xdecref(entry)

    def put_var_xdecrefs_clear(self, entries):
        for entry in entries:
            self.put_var_xdecref_clear(entry)

    def put_init_to_py_none(self, cname, type, nanny=True):
        from PyrexTypes import py_object_type, typecast
        py_none = typecast(type, py_object_type, "Py_None")
        if nanny:
            self.putln("%s = %s; __Pyx_INCREF(Py_None);" % (cname, py_none))
        else:
            self.putln("%s = %s; Py_INCREF(Py_None);" % (cname, py_none))

    def put_init_var_to_py_none(self, entry, template = "%s", nanny=True):
        code = template % entry.cname
        #if entry.type.is_extension_type:
        #    code = "((PyObject*)%s)" % code
        self.put_init_to_py_none(code, entry.type, nanny)
        if entry.in_closure:
            self.put_giveref('Py_None')

    def put_pymethoddef(self, entry, term, allow_skip=True):
        if entry.is_special or entry.name == '__getattribute__':
            if entry.name not in ['__cinit__', '__dealloc__', '__richcmp__', '__next__', '__getreadbuffer__', '__getwritebuffer__', '__getsegcount__', '__getcharbuffer__', '__getbuffer__', '__releasebuffer__']:
                if entry.name == '__getattr__' and not self.globalstate.directives['fast_getattr']:
                    pass
                # Python's typeobject.c will automatically fill in our slot
                # in add_operators() (called by PyType_Ready) with a value
                # that's better than ours.
                elif allow_skip:
                    return
        from TypeSlots import method_coexist
        if entry.doc:
            doc_code = entry.doc_cname
        else:
            doc_code = 0
        method_flags = entry.signature.method_flags()
        if method_flags:
            if entry.is_special:
                method_flags += [method_coexist]
            self.putln(
                '{__Pyx_NAMESTR("%s"), (PyCFunction)%s, %s, __Pyx_DOCSTR(%s)}%s' % (
                    entry.name,
                    entry.func_cname,
                    "|".join(method_flags),
                    doc_code,
                    term))

    # GIL methods

    def put_ensure_gil(self, declare_gilstate=True):
        """
        Acquire the GIL. The generated code is safe even when no PyThreadState
        has been allocated for this thread (for threads not initialized by
        using the Python API). Additionally, the code generated by this method
        may be called recursively.
        """
        from Cython.Compiler import Nodes

        self.globalstate.use_utility_code(Nodes.force_init_threads_utility_code)

        self.putln("#ifdef WITH_THREAD")
        if declare_gilstate:
            self.put("PyGILState_STATE ")
        self.putln("__pyx_gilstate_save = PyGILState_Ensure();")
        self.putln("#endif")

    def put_release_ensured_gil(self):
        """
        Releases the GIL, corresponds to `put_ensure_gil`.
        """
        self.putln("#ifdef WITH_THREAD")
        self.putln("PyGILState_Release(__pyx_gilstate_save);")
        self.putln("#endif")

    def put_acquire_gil(self):
        """
        Acquire the GIL. The thread's thread state must have been initialized
        by a previous `put_release_gil`
        """
        self.putln("Py_BLOCK_THREADS")

    def put_release_gil(self):
        "Release the GIL, corresponds to `put_acquire_gil`."
        self.putln("#ifdef WITH_THREAD")
        self.putln("PyThreadState *_save = NULL;")
        self.putln("#endif")
        self.putln("Py_UNBLOCK_THREADS")

    def declare_gilstate(self):
        self.putln("#ifdef WITH_THREAD")
        self.putln("PyGILState_STATE __pyx_gilstate_save;")
        self.putln("#endif")

    # error handling

    def put_error_if_neg(self, pos, value):
#        return self.putln("if (unlikely(%s < 0)) %s" % (value, self.error_goto(pos)))  # TODO this path is almost _never_ taken, yet this macro makes is slower!
        return self.putln("if (%s < 0) %s" % (value, self.error_goto(pos)))

    def put_error_if_unbound(self, pos, entry):
        import ExprNodes
        if entry.from_closure:
            func = '__Pyx_RaiseClosureNameError'
            self.globalstate.use_utility_code(
                ExprNodes.raise_closure_name_error_utility_code)
        else:
            func = '__Pyx_RaiseUnboundLocalError'
            self.globalstate.use_utility_code(
                ExprNodes.raise_unbound_local_error_utility_code)
        self.put('if (unlikely(!%s)) { %s("%s"); %s }' % (
            entry.cname, func, entry.name, self.error_goto(pos)))

    def set_error_info(self, pos):
        self.funcstate.should_declare_error_indicator = True
        if self.c_line_in_traceback:
            cinfo = " %s = %s;" % (Naming.clineno_cname, Naming.line_c_macro)
        else:
            cinfo = ""

        return "%s = %s[%s]; %s = %s;%s" % (
            Naming.filename_cname,
            Naming.filetable_cname,
            self.lookup_filename(pos[0]),
            Naming.lineno_cname,
            pos[1],
            cinfo)

    def error_goto(self, pos):
        lbl = self.funcstate.error_label
        self.funcstate.use_label(lbl)
        return "{%s goto %s;}" % (
            self.set_error_info(pos),
            lbl)

    def error_goto_if(self, cond, pos):
        return "if (%s) %s" % (self.unlikely(cond), self.error_goto(pos))

    def error_goto_if_null(self, cname, pos):
        return self.error_goto_if("!%s" % cname, pos)

    def error_goto_if_neg(self, cname, pos):
        return self.error_goto_if("%s < 0" % cname, pos)

    def error_goto_if_PyErr(self, pos):
        return self.error_goto_if("PyErr_Occurred()", pos)

    def lookup_filename(self, filename):
        return self.globalstate.lookup_filename(filename)

    def put_declare_refcount_context(self):
        self.putln('__Pyx_RefNannyDeclarations')

    def put_setup_refcount_context(self, name):
        self.putln('__Pyx_RefNannySetupContext("%s");' % name)

    def put_finish_refcount_context(self):
        self.putln("__Pyx_RefNannyFinishContext();")

    def put_add_traceback(self, qualified_name):
        """
        Build a Python traceback for propagating exceptions.

        qualified_name should be the qualified name of the function
        """
        format_tuple = (
            qualified_name,
            Naming.clineno_cname,
            Naming.lineno_cname,
            Naming.filename_cname,
        )
        self.putln('__Pyx_AddTraceback("%s", %s, %s, %s);' % format_tuple)

    def put_trace_declarations(self):
        self.putln('__Pyx_TraceDeclarations');

    def put_trace_call(self, name, pos):
        self.putln('__Pyx_TraceCall("%s", %s[%s], %s);' % (name, Naming.filetable_cname, self.lookup_filename(pos[0]), pos[1]));

    def put_trace_exception(self):
        self.putln("__Pyx_TraceException();")

    def put_trace_return(self, retvalue_cname):
        self.putln("__Pyx_TraceReturn(%s);" % retvalue_cname)

    def putln_openmp(self, string):
        self.putln("#ifdef _OPENMP")
        self.putln(string)
        self.putln("#endif /* _OPENMP */")

class PyrexCodeWriter(object):
    # f                file      output file
    # level            int       indentation level

    def __init__(self, outfile_name):
        self.f = Utils.open_new_file(outfile_name)
        self.level = 0

    def putln(self, code):
        self.f.write("%s%s\n" % (" " * self.level, code))

    def indent(self):
        self.level += 1

    def dedent(self):
        self.level -= 1


class ClosureTempAllocator(object):
    def __init__(self, klass):
        self.klass = klass
        self.temps_allocated = {}
        self.temps_free = {}
        self.temps_count = 0

    def reset(self):
        for type, cnames in self.temps_allocated.items():
            self.temps_free[type] = list(cnames)

    def allocate_temp(self, type):
        if not type in self.temps_allocated:
            self.temps_allocated[type] = []
            self.temps_free[type] = []
        elif self.temps_free[type]:
            return self.temps_free[type].pop(0)
        cname = '%s%d' % (Naming.codewriter_temp_prefix, self.temps_count)
        self.klass.declare_var(pos=None, name=cname, cname=cname, type=type, is_cdef=True)
        self.temps_allocated[type].append(cname)
        self.temps_count += 1
        return cname
