# pyflyby/_parse.py.
# Copyright (C) 2011, 2012, 2013, 2014 Karl Chen.
# License: MIT http://opensource.org/licenses/MIT

from __future__ import absolute_import, division, with_statement

import ast
from   itertools                import groupby
import re
import sys
from   textwrap                 import dedent
import types

from   pyflyby._file            import FilePos, FileText, Filename
from   pyflyby._flags           import CompilerFlags
from   pyflyby._log             import logger
from   pyflyby._util            import cached_attribute


def _is_comment_or_blank(line):
    """
    Returns whether a line of python code contains only a comment is blank.

      >>> _is_comment_or_blank("foo\\n")
      False

      >>> _is_comment_or_blank("  # blah\\n")
      True
    """
    return re.sub("#.*", "", line).rstrip() == ""


def _ast_str_literal_value(node):
    if isinstance(node, ast.Str):
        return node.s
    if isinstance(node, ast.Expr) and isinstance(node.value, ast.Str):
        return node.value.s
    else:
        return None


def _flatten_ast_nodes(arg):
    if arg is None:
        pass
    elif isinstance(arg, ast.AST):
        yield arg
    elif isinstance(arg, (tuple, list, types.GeneratorType)):
        for x in arg:
            for y in _flatten_ast_nodes(x):
                yield y
    else:
        raise TypeError(
            "_flatten_ast_nodes: unexpected %s" % (type(arg).__name__,))


def _iter_child_nodes_in_order(node):
    """
    Yield all direct child nodes of C{node}, that is, all fields that are nodes
    and all items of fields that are lists of nodes.

    C{_iter_child_nodes_in_order} yields nodes in the same order that they
    appear in the source.

    C{ast.iter_child_nodes} does the same thing, but not in source order.
    e.g. for C{Dict}s, it yields all key nodes before all value nodes.
    """
    return _flatten_ast_nodes(_iter_child_nodes_in_order_internal_1(node))


def _iter_child_nodes_in_order_internal_1(node):
    if not isinstance(node, ast.AST):
        raise TypeError
    if isinstance(node, ast.Dict):
        assert node._fields == ("keys", "values")
        yield zip(node.keys, node.values)
    elif isinstance(node, ast.FunctionDef):
        assert node._fields == ('name', 'args', 'body', 'decorator_list')
        yield node.decorator_list, node.args, node.body
        # node.name is a string, not an AST node
    elif isinstance(node, ast.arguments):
        assert node._fields == ('args', 'vararg', 'kwarg', 'defaults')
        defaults = node.defaults or ()
        num_no_default = len(node.args)-len(defaults)
        yield node.args[:num_no_default]
        yield zip(node.args[num_no_default:], defaults)
        # node.varags and node.kwarg are strings, not AST nodes.
    elif isinstance(node, ast.IfExp):
        assert node._fields == ('test', 'body', 'orelse')
        yield node.body, node.test, node.orelse
    elif isinstance(node, ast.ClassDef):
        assert node._fields == ('name', 'bases', 'body', 'decorator_list')
        yield node.decorator_list, node.bases, node.body
        # node.name is a string, not an AST node
    else:
        # Default behavior.
        yield ast.iter_child_nodes(node)


def _walk_ast_nodes_in_order(node):
    """
    Recursively yield all child nodes of C{node}, in the same order that the
    node appears in the source.

    C{ast.walk} does the same thing, but yields nodes in an arbitrary order.
    """
    # The implementation is basically the same as C{ast.walk}, but:
    #   1. Use a stack instead of a deque.  (I.e., depth-first search instead
    #      of breadth-first search.)
    #   2. Use _iter_child_nodes_in_order instead of C{ast.walk}.
    todo = [node]
    while todo:
        node = todo.pop()
        yield node
        todo.extend(reversed(list(_iter_child_nodes_in_order(node))))


def _parse_ast_nodes(text, flags, mode):
    """
    Parse a block of lines into an AST.

    @type text:
      C{FileText}
    @type flags:
      C{CompilerFlags}
    @rtype:
      C{ast.Module}
    """
    text = FileText(text)
    flags = CompilerFlags(flags)
    filename = str(text.filename) if text.filename else "<unknown>"
    source = text.joined
    source = dedent(source)
    if not source.endswith("\n"):
        # Ensure that the last line ends with a newline (C{ast} barfs
        # otherwise).
        source += "\n"
    flags = ast.PyCF_ONLY_AST | int(flags)
    return compile(source, filename, mode, flags=flags, dont_inherit=1)


def _test_parse_string_literal(text, flags):
    r"""
    Attempt to parse C{text}.  If it parses cleanly to a single string
    literal, return its value.  Otherwise return C{None}.

      >>> _test_parse_string_literal(r'"foo\n" r"\nbar"', 0)
      'foo\n\\nbar'

    """
    try:
        module_node = _parse_ast_nodes(text, flags, "eval")
    except SyntaxError:
        return None
    body = module_node.body
    if not isinstance(body, ast.Str):
        return None
    return body.s


def _parse_annotate_ast_nodes(text, flags):
    """
    Parse a block of lines into an AST and annotate with startpos and endpos.

    @type text:
      C{FileText}
    @type flags:
      C{CompilerFlags}
    @rtype:
      C{ast.Module}
    """
    text = FileText(text)
    flags = CompilerFlags(flags)
    ast_node = _parse_ast_nodes(text, flags, "exec")
    # Annotate starting line numbers.
    _annotate_ast_startpos(ast_node, text.startpos, text, flags)
    return ast_node


def _annotate_ast_startpos(ast_node, minpos, text, flags):
    """
    Annotate C{ast_node}.  Set C{ast_node.startpos} to the starting position
    of the node within C{text}.

    For "typical" nodes, i.e. those other than multiline strings, this is
    simply FilePos(ast_node.lineno, ast_node.col_offset+1), but taking
    C{text.startpos} into account.

    For multiline string nodes, this function works by trying to parse all
    possible subranges of lines until finding the range that is syntactically
    valid and matches C{value}.  The candidate range is
    text[min_start_lineno:lineno+text.startpos.lineno+1].

    This function is unfortunately necessary because of a flaw in the output
    produced by the Python built-in parser.  For some crazy reason, the
    C{ast_node.lineno} attribute represents something different for multiline
    string literals versus all other statements.  For multiline string literal
    nodes and statements that are just a string expression (or more generally,
    nodes where the first descendant leaf node is a multiline string literal),
    the compiler attaches the ending line number as the value of the C{lineno}
    attribute.  For all other than AST nodes, the compiler attaches the
    starting line number as the value of the C{lineno} attribute.  This means
    e.g. the statement "'''foo\nbar'''" has a lineno value of 2, but the
    statement "x='''foo\nbar'''" has a lineno value of 1.

    @type ast_node:
      C{ast.AST}
    @type minpos:
      L{FilePos}
    @param minpos:
      Earliest position to check, in the number space of C{text}.
    @type text:
      L{FileText}
    @param text:
      Source text that was used to parse the AST, whose C{startpos} should be
      used in interpreting C{ast_node.lineno} (which always starts at 1 for
      the subset that was parsed).
    @type flags:
      C{CompilerFlags}
    @param flags:
      Compiler flags to use when re-compiling code.
    @return:
      C{True} if this node is a multiline string literal or the first child is
      such a node (recursively); C{False} otherwise.
    @raise ValueError:
      Could not find the starting line number.
    """
    # First, traverse child nodes.  If the first child node (recursively) is a
    # multiline string, then we need to transfer its information to this node.
    # Walk all nodes/fields of the AST.  We implement this as a custom
    # depth-first search instead of using ast.walk() or ast.NodeVisitor
    # so that we can easily keep track of the preceding node's lineno.
    child_minpos = minpos
    is_first_child = True
    leftstr_node = None
    for child_node in _iter_child_nodes_in_order(ast_node):
        leftstr = _annotate_ast_startpos(child_node, child_minpos, text, flags)
        if is_first_child and leftstr:
            leftstr_node = child_node
        if hasattr(child_node, 'lineno'):
            if child_node.startpos < child_minpos:
                raise AssertionError(
                    "Got out-of-order AST node(s):\n"
                    "  parent minpos=%s\n" % minpos +
                    "    node: %s\n" % ast.dump(ast_node) +
                    "      fields: %s\n" % (" ".join(ast_node._fields)) +
                    "      children:\n" +
                    ''.join(
                        "        %s %9s: %s\n" % (
                            ("==>" if cn is child_node else "   "),
                            getattr(cn, 'startpos', ""),
                            ast.dump(cn))
                        for cn in _iter_child_nodes_in_order(ast_node)) +
                    "\n"
                    "This indicates a bug in pyflyby._\n"
                    "\n"
                    "pyflyby developer: Check if there's a bug or missing ast node handler in "
                    "pyflyby._parse._iter_child_nodes_in_order() - "
                    "probably the handler for ast.%s." % type(ast_node).__name__)
            child_minpos = child_node.startpos
        is_first_child = False
    # If the node has no lineno at all, then skip it.  This should only happen
    # for nodes we don't care about, e.g. C{ast.Module} or C{ast.alias}.
    if not hasattr(ast_node, 'lineno'):
        return False
    # If col_offset is set then the lineno should be correct also.
    if ast_node.col_offset >= 0:
        # Not a multiline string literal.  (I.e., it could be a non-string or
        # a single-line string.)
        # Easy.
        delta = (ast_node.lineno-1, ast_node.col_offset)
        ast_node.startpos = text.startpos + delta
        return False
    assert ast_node.col_offset == -1
    if leftstr_node:
        # This is an ast node where the leftmost deepest leaf is a
        # multiline string.  The bug that multiline strings have broken
        # lineno/col_offset infects ancestors up the tree.
        #
        # If the leftmost leaf is a multi-line string, then C{lineno}
        # contains the ending line number, and col_offset is -1:
        #   >>> ast.parse("""'''foo\nbar'''+blah""").body[0].lineno
        #   2
        # But if the leftmost leaf is not a multi-line string, then
        # C{lineno} contains the starting line number:
        #   >>> ast.parse("""'''foobar'''+blah""").body[0].lineno
        #   1
        #   >>> ast.parse("""blah+'''foo\nbar'''+blah""").body[0].lineno
        #   1
        #
        # To fix that, we copy start_lineno and start_colno from the Str
        # node once we've corrected the values.
        assert not isinstance(ast_node, ast.Str)
        assert leftstr_node.lineno     == ast_node.lineno
        assert leftstr_node.col_offset == -1
        ast_node.startpos = leftstr_node.startpos
        return True
    # It should now be the case that we are looking at a multi-line string
    # literal.
    if not isinstance(ast_node, ast.Str):
        raise ValueError(
            "got a non-string col_offset=-1: %s" % (ast.dump(ast_node)))
    # The C{lineno} attribute gives the ending line number of the multiline
    # string ... unless it's multiple multiline strings that are concatenated
    # by adjacency, in which case it's merely the end of the first one of
    # them.  At least we know that the start lineno is definitely not later
    # than the C{lineno} attribute.
    first_end_lineno = text.startpos.lineno + ast_node.lineno - 1
    # Compute possible start positions.
    # The starting line number of this string could be anywhere between the
    # end of the previous expression and C{first_end_lineno}.
    startpos_candidates = []
    assert minpos.lineno <= first_end_lineno
    for start_lineno in range(minpos.lineno, first_end_lineno + 1):
        start_line = text[start_lineno]
        start_line_colno = (text.startpos.colno
                            if start_lineno==text.startpos.lineno else 1)
        startpos_candidates.extend([
            (m.group()[-1], FilePos(start_lineno, m.start()+start_line_colno))
            for m in re.finditer("[bBrRuU]*[\"\']", start_line)])
    target_str = ast_node.s
    # Loop over possible end_linenos.  The first one we've identified is the
    # by far most likely one, but in theory it could be anywhere later in the
    # file.  This could be because of a dastardly concatenated string like
    # this:
    #   """       # L1
    #   two       # L2
    #   """   """ # L3
    #   four      # L4
    #   five      # L5
    #   six       # L6
    #   """       # L7
    # There are two substrings on L1:L3 and L3:L7.  The parser gives us a
    # single concatenated string, but sets lineno to 3 instead of 7.  We don't
    # have much to go on to figure out that the real end_lineno is 7.  If we
    # don't find the string ending on L3, then search forward looking for the
    # real end of the string.  Yuck!
    for end_lineno in xrange(first_end_lineno, text.endpos.lineno+1):
        # Compute possible end positions.  We're given the line we're ending
        # on, but not the column position.  Note that the ending line could
        # contain more than just the string we're looking for -- including
        # possibly other strings or comments.
        end_line = text[end_lineno]
        end_line_startcol = (
            text.startpos.colno if end_lineno==text.startpos.lineno else 1)
        endpos_candidates = [
            (m.group(), FilePos(end_lineno,m.start()+end_line_startcol+1))
            for m in re.finditer("[\"\']", end_line)]
        if not endpos_candidates:
            # We found no endpos_candidates.  This should not happen for
            # first_end_lineno because there should be _some_ string that ends
            # there.
            if end_lineno == first_end_lineno:
                raise AssertionError(
                    "No quote char found on line with supposed string")
            continue
        # Filter and sort the possible startpos candidates given this endpos
        # candidate.  It's possible for the starting quotechar and ending
        # quotechar to be different in case of adjacent string concatenation,
        # e.g.  "foo"'''bar'''.  That said, it's an unlikely case, so
        # deprioritize checking them.
        likely_candidates = []
        unlikely_candidates = []
        for end_quotechar, endpos in reversed(endpos_candidates):
            for start_quotechar, startpos in startpos_candidates:
                if not startpos < endpos:
                    continue
                if start_quotechar == end_quotechar:
                    candidate_list = likely_candidates
                else:
                    candidate_list = unlikely_candidates
                candidate_list.append((startpos,endpos))
        # Loop over sorted candidates.
        matched_prefix = set()
        for (startpos, endpos) in likely_candidates + unlikely_candidates:
            # Try to parse the given range and see if it matches the target
            # string literal.
            subtext = text[startpos:endpos]
            candidate_str = _test_parse_string_literal(subtext, flags)
            if candidate_str is None:
                continue
            elif target_str == candidate_str:
                # Success!
                ast_node.startpos = startpos
                ast_node.endpos   = endpos
                # This node is a multiline string; and, it's a leaf, so by
                # definition it is the leftmost node.
                return True # all done
            elif target_str.startswith(candidate_str):
                matched_prefix.add(startpos)
        # We didn't find a string given the current end_lineno candidate.
        # Only continue checking the startpos candidates that so far produced
        # prefixes of the string we're looking for.
        if not matched_prefix:
            break
        startpos_candidates = [
            (sq, sp)
            for (sq, sp) in startpos_candidates
            if sp in matched_prefix
        ]
    raise ValueError(
        "Couldn't find exact position of %s"
        % (ast.dump(ast_node)))


def _split_code_lines(ast_nodes, text):
    """
    Split the given C{ast_nodes} and corresponding C{text} by code/noncode
    statement.

    Yield tuples of (nodes, subtext).  C{nodes} is a list of C{ast.AST} nodes,
    length 0 or 1; C{subtext} is a L{FileText} sliced from C{text}.

    FileText(...))} for code lines and C{(None, FileText(...))} for non-code
    lines (comments and blanks).

    @type ast_nodes:
      sequence of C{ast.AST} nodes
    @type text:
      L{FileText}
    """
    if not ast_nodes:
        yield ([], text)
        return
    assert text.startpos <= ast_nodes[0].startpos
    assert ast_nodes[-1].startpos < text.endpos
    if text.startpos != ast_nodes[0].startpos:
        # Starting noncode lines.
        yield ([], text[text.startpos:ast_nodes[0].startpos])
    end_sentinel = _DummyAst_Node()
    end_sentinel.startpos = text.endpos
    for node, next_node in zip(ast_nodes, ast_nodes[1:] + [end_sentinel]):
        startpos = node.startpos
        next_startpos = next_node.startpos
        assert startpos < next_startpos
        # We have the start position of this node.  Figure out the end
        # position, excluding noncode lines (standalone comments and blank
        # lines).
        if hasattr(node, 'endpos'):
            # We have an endpos for the node because this was a multi-line
            # string.  Start with the node endpos.
            endpos = node.endpos
            assert startpos < endpos <= next_startpos
            # enpos points to the character *after* the ending quote, so we
            # know that this is never at the beginning of the line.
            assert endpos.colno != 1
            # Advance past whitespace an inline comment, if any.  Do NOT
            # advance past other code that could be on the same line, nor past
            # blank lines and comments on subsequent lines.
            line = text[endpos : min(text.endpos, FilePos(endpos.lineno+1,1))]
            if _is_comment_or_blank(line):
                endpos = FilePos(endpos.lineno+1, 1)
        else:
            endpos = next_startpos
            assert endpos <= text.endpos
            # We don't have an endpos yet; what we do have is the next node's
            # startpos (or the position at the end of the text).  Start there
            # and work backward.
            if endpos.colno != 1:
                if endpos == text.endpos:
                    # There could be a comment on the last line and no
                    # trailing newline.
                    # TODO: do this in a more principled way.
                    if _is_comment_or_blank(text[endpos.lineno]):
                        assert startpos.lineno < endpos.lineno
                        if not text[endpos.lineno-1].endswith("\\"):
                            endpos = FilePos(endpos.lineno,1)
                else:
                    # We're not at end of file, yet the next node starts in
                    # the middle of the line.  This should only happen with if
                    # we're not looking at a comment.  [The first character in
                    # the line could still be "#" if we're inside a multiline
                    # string that's the last child of the parent node.
                    # Therefore we don't assert 'not
                    # _is_comment_or_blank(...)'.]
                    pass
            if endpos.colno == 1:
                while (endpos.lineno-1 > startpos.lineno and
                       _is_comment_or_blank(text[endpos.lineno-1]) and
                       not text[endpos.lineno-2].endswith("\\")):
                    endpos = FilePos(endpos.lineno-1, 1)
        assert startpos < endpos <= next_startpos
        yield ([node], text[startpos:endpos])
        if endpos != next_startpos:
            yield ([], text[endpos:next_startpos])


class _DummyAst_Node(object):
    pass


class PythonStatement(object):
    r"""
    Representation of a top-level Python statement or consecutive
    comments/blank lines.

      >>> PythonStatement('print("x",\n file=None)\n', flags=0x10000)
      PythonStatement('print("x",\n file=None)\n', flags=0x10000)

    Implemented as a wrapper around a L{PythonBlock} containing at most one
    top-level AST node.
    """

    def __new__(cls, arg, filename=None, startpos=None, flags=None):
        if isinstance(arg, cls):
            if filename is startpos is flags is None:
                return arg
            arg = arg.block
            # Fall through
        if isinstance(arg, (PythonBlock, FileText, str)):
            block = PythonBlock(arg, filename=filename,
                                startpos=startpos, flags=flags)
            statements = block.statements
            if len(statements) != 1:
                raise ValueError(
                    "Code contains %d statements instead of exactly 1: %r"
                    % (len(statements), block))
            statement, = statements
            assert isinstance(statement, cls)
            return statement
        raise TypeError("PythonStatement: unexpected %s" % (type(arg).__name__,))

    @classmethod
    def _construct_from_block(cls, block):
        # Only to be used by PythonBlock.
        assert isinstance(block, PythonBlock)
        self = object.__new__(cls)
        self.block = block
        return self

    @property
    def text(self):
        """
        @rtype:
          L{FileText}
        """
        return self.block.text

    @property
    def filename(self):
        """
        @rtype:
          L{Filename}
        """
        return self.text.filename

    @property
    def startpos(self):
        """
        @rtype:
          L{FilePos}
        """
        return self.text.startpos

    @property
    def flags(self):
        """
        @rtype:
          L{CompilerFlags}
        """
        return self.block.flags

    @property
    def ast_node(self):
        """
        A single AST node representing this statement, or C{None} if this
        object only represents comments/blanks.

        @rtype:
          C{ast.AST} or C{NoneType}
        """
        ast_nodes = self.block.ast_node.body
        if len(ast_nodes) == 0:
            return None
        if len(ast_nodes) == 1:
            return ast_nodes[0]
        raise AssertionError("More than one AST node in block")

    @property
    def is_comment_or_blank(self):
        return self.ast_node is None

    @property
    def is_comment_or_blank_or_string_literal(self):
        return (self.is_comment_or_blank
                or _ast_str_literal_value(self.ast_node) is not None)

    @property
    def is_import(self):
        return isinstance(self.ast_node, (ast.Import, ast.ImportFrom))

    @property
    def is_single_assign(self):
        n = self.ast_node
        return isinstance(n, ast.Assign) and len(n.targets) == 1

    def get_assignment_literal_value(self):
        """
        If the statement is an assignment, return the name and literal value.

          >>> PythonStatement('foo = {1: {2: 3}}').get_assignment_literal_value()
          ('foo', {1: {2: 3}})

        @return:
          (target, literal_value)
        """
        if not self.is_single_assign:
            raise ValueError(
                "Statement is not an assignment to a single name: %s" % self)
        n = self.ast_node
        target_name = n.targets[0].id
        literal_value = ast.literal_eval(n.value)
        return (target_name, literal_value)

    def __repr__(self):
        r = repr(self.block)
        assert r.startswith("PythonBlock(")
        r = "PythonStatement(" + r[12:]
        return r

    def __eq__(self, other):
        if self is other:
            return True
        if not isinstance(other, PythonStatement):
            return NotImplemented
        return self.block == other.block

    def __ne__(self, other):
        if self is other:
            return False
        if not isinstance(other, PythonStatement):
            return NotImplemented
        return self.block != other.block

    def __cmp__(self, other):
        if self is other:
            return 0
        if not isinstance(other, PythonStatement):
            return NotImplemented
        return cmp(self.block, other.block)

    def __hash__(self):
        return hash(self.block)


class PythonBlock(object):
    r"""
    Representation of a sequence of consecutive top-level
    L{PythonStatement}(s).

      >>> source_code = '# 1\nprint 2\n# 3\n# 4\nprint 5\nx=[6,\n 7]\n# 8\n'
      >>> codeblock = PythonBlock(source_code)
      >>> for stmt in PythonBlock(codeblock).statements:
      ...     print stmt
      PythonStatement('# 1\n')
      PythonStatement('print 2\n', startpos=(2,1))
      PythonStatement('# 3\n# 4\n', startpos=(3,1))
      PythonStatement('print 5\n', startpos=(5,1))
      PythonStatement('x=[6,\n 7]\n', startpos=(6,1))
      PythonStatement('# 8\n', startpos=(8,1))

    A C{PythonBlock} has a C{flags} attribute that gives the compiler_flags
    associated with the __future__ features using which the code should be
    parsed.

    """

    def __new__(cls, arg, filename=None, startpos=None, flags=None):
        if isinstance(arg, PythonStatement):
            arg = arg.block
            # Fall through
        if isinstance(arg, cls):
            if filename is startpos is flags is None:
                return arg
            flags = CompilerFlags(flags, arg.flags)
            arg = arg.text
            # Fall through
        if isinstance(arg, (FileText, Filename, str)):
            return cls.from_text(
                arg, filename=filename, startpos=startpos, flags=flags)
        raise TypeError("%s: unexpected %s"
                        % (cls.__name__, type(arg).__name__,))

    @classmethod
    def from_filename(cls, filename):
        return cls.from_text(Filename(filename))

    @classmethod
    def from_text(cls, text, filename=None, startpos=None, flags=None):
        """
        @type text:
          L{FileText} or convertible
        @rtype:
          L{PythonBlock}
        """
        text = FileText(text, filename=filename, startpos=startpos)
        self = object.__new__(cls)
        self.text = text
        self._input_flags = CompilerFlags(flags)
        return self

    @classmethod
    def __construct_from_ast(cls, ast_nodes, text, flags):
        # Constructor for internal use by _split_by_statement() or
        # concatenate().
        self = object.__new__(cls)
        self.ast_node = ast.Module(ast_nodes)
        self.text     = text
        self.flags    = self._input_flags = flags
        return self

    @classmethod
    def concatenate(cls, blocks, assume_contiguous=False):
        """
        Concatenate a bunch of blocks into one block.

        @type blocks:
          sequence of L{PythonBlock}s and/or L{PythonStatement}s
        @param assume_contiguous:
          Whether to assume, without checking, that the input blocks were
          originally all contiguous.  This must be set to True to indicate the
          caller understands the assumption; False is not implemented.
        """
        if not assume_contiguous:
            raise NotImplementedError
        blocks = [PythonBlock(b) for b in blocks]
        if len(blocks) == 1:
            return blocks[0]
        assert blocks
        text = FileText.concatenate([b.text for b in blocks])
        # The contiguous assumption is important here because C{ast_node}
        # contains line information that would otherwise be wrong.
        ast_nodes = [n for b in blocks for n in b.ast_node.body]
        flags = blocks[0].flags
        return cls.__construct_from_ast(ast_nodes, text, flags)

    @property
    def filename(self):
        return self.text.filename

    @property
    def startpos(self):
        return self.text.startpos

    @property
    def endpos(self):
        return self.text.endpos

    @cached_attribute
    def ast_node(self):
        """
        Parse this block of code into an abstract syntax tree.

        The returned object type is the kind of AST as returned by the
        C{compile} built-in (rather than as returned by the older, deprecated
        C{compiler} module).  The code is parsed using mode="exec".

        All nodes are annotated with C{startpos}.
        All top-level nodes are annotated with C{endpos}.

        The result is a C{ast.Module} node, even if this block represents only
        a subset of the entire file.

        @rtype:
          C{ast.Module}
        """
        if hasattr(self, "_failed_compile"):
            raise self._failed_compile
        # ast_node may also be set directly by __construct_from_ast(),
        # in which case this code does not run.
        try:
            return _parse_annotate_ast_nodes(self.text, self._input_flags)
        except Exception as e:
            # Add the filename to the exception message to be nicer.
            if self.text.filename:
                e = type(e)("While parsing %s: %s" % (self.text.filename, e))
            # Cache the exception to avoid re-attempting while debugging.
            self._failed_compile = e
            raise e, None, sys.exc_info()[2]

    def parse(self, mode):
        """
        Parse the source text into an AST.

        @param mode:
          Compilation mode: "exec", "single", or "eval".  If "exec", consider
          using C{ast_node} instead, which is cached and annotates line
          numbers.
        @rtype:
          C{ast.AST}
        """
        return _parse_ast_nodes(self.text, self._input_flags, mode)

    @cached_attribute
    def statements(self):
        r"""
        Partition of this C{PythonBlock} into individual C{PythonStatement}s.
        Each one contains at most 1 top-level ast node.  A C{PythonStatement}
        can contain no ast node to represent comments.

          >>> code = "# multiline\n# comment\n'''multiline\nstring'''\nblah\n"
          >>> print PythonBlock(code).statements # doctest:+NORMALIZE_WHITESPACE
          (PythonStatement('# multiline\n# comment\n'),
           PythonStatement("'''multiline\nstring'''\n", startpos=(3,1)),
           PythonStatement('blah\n', startpos=(5,1)))

        @rtype:
          C{tuple} of L{PythonStatement}s
        """
        nodes_subtexts = list(_split_code_lines(self.ast_node.body, self.text))
        if nodes_subtexts == [(self.ast_node.body, self.text)]:
            # This block is either all comments/blanks or a single statement
            # with no surrounding whitespace/comment lines.  Return self.
            return (PythonStatement._construct_from_block(self),)
        cls = type(self)
        statement_blocks = [
            cls.__construct_from_ast(subnodes, subtext, self.flags)
            for subnodes, subtext in nodes_subtexts]
        # Convert to statements.
        statements = []
        for b in statement_blocks:
            statement = PythonStatement._construct_from_block(b)
            statements.append(statement)
            # Optimization: set the new sub-block's C{statements} attribute
            # since we already know it contains exactly one statement, itself.
            assert 'statements' not in b.__dict__
            b.statements = (statement,)
        return tuple(statements)

    @cached_attribute
    def source_flags(self):
        """
        If the AST contains __future__ imports, then the compiler_flags
        associated with them.  Otherwise, 0.

        The difference between C{source_flags} and C{flags} is that C{flags}
        may be set by the caller (e.g. based on an earlier __future__ import),
        whereas C{source_flags} is only nonzero if this code itself contains
        __future__ imports.

        @rtype:
          L{CompilerFlags}
        """
        return CompilerFlags.from_ast(self.ast_node)

    @cached_attribute
    def flags(self):
        """
        The compiler flags for this code block, including both the input flags
        and the source flags.

        @rtype:
          L{CompilerFlags}
        """
        return self._input_flags | self.source_flags

    @cached_attribute
    def parse_tree(self):
        """
        Return an C{AST} as returned by the C{compiler} module.
        """
        # Note that the 'compiler' module is deprecated, which is why we use
        # the C{compile} built-in above.  This is for interfacing with
        # pyflakes 0.4 and earlier.
        import compiler
        joined = self.text.joined
        if not joined.endswith("\n"):
            # Ensure that the last line ends with a newline (C{parse} barfs
            # otherwise).
            joined += "\n"
        return compiler.parse(joined)

    def __repr__(self):
        r = "%s(%r" % (type(self).__name__, self.text.joined)
        if self.filename:
            r += ", filename=%r" % (str(self.filename),)
        if self.startpos != FilePos():
            r += ", startpos=%s" % (self.startpos,)
        if self.flags != self.source_flags:
            r += ", flags=%s" % (self.flags,)
        r += ")"
        return r

    def groupby(self, predicate):
        """
        Partition this block of code into smaller blocks of code which
        consecutively have the same C{predicate}.

        @param predicate:
          Function that takes a L{PythonStatement} and returns a value.
        @return:
          Generator that yields (group, L{PythonBlock}s).
        """
        cls = type(self)
        for pred, stmts in groupby(self.statements, predicate):
            blocks = [s.block for s in stmts]
            yield pred, cls.concatenate(blocks, assume_contiguous=True)

    def string_literals(self):
        r"""
        Yield all string literals anywhere in this block.

        The string literals have C{startpos} attributes attached.

          >>> block = PythonBlock("'a' + ('b' + \n'c')")
          >>> [(f.s, f.startpos) for f in block.string_literals()]
          [('a', FilePos(1,1)), ('b', FilePos(1,8)), ('c', FilePos(2,1))]

        @return:
          Iterable of C{ast.Str} nodes
        """
        for node in _walk_ast_nodes_in_order(self.ast_node):
            if isinstance(node, ast.Str):
                assert hasattr(node, 'startpos')
                yield node

    def get_doctests(self):
        r"""
        Return doctests in this code.

          >>> PythonBlock("x\n'''\n >>> foo(bar\n ...     + baz)\n'''\n").get_doctests()
          [PythonBlock('foo(bar\n    + baz)\n', startpos=(3,2))]

        @rtype:
          C{list} of L{PythonStatement}s
        """
        import doctest
        parser = doctest.DocTestParser()
        doctest_blocks = []
        filename = self.filename
        flags = self.flags
        for ast_node in self.string_literals():
            try:
                examples = parser.get_examples(ast_node.s)
            except Exception:
                blob = ast_node.s
                if len(blob) > 60:
                    blob = blob[:60] + '...'
                # TODO: let caller decide how to handle
                logger.warning("Can't parse docstring; ignoring: %r", blob)
                continue
            for example in examples:
                lineno = ast_node.startpos.lineno + example.lineno
                colno = ast_node.startpos.colno + example.indent # dubious
                text = FileText(example.source, filename=filename,
                                startpos=(lineno,colno))
                try:
                    block = PythonBlock(text, flags=flags)
                    block.ast_node # make sure we can parse
                except Exception:
                    blob = text.joined
                    if len(blob) > 60:
                        blob = blob[:60] + '...'
                    logger.warning("Can't parse doctest; ignoring: %r", blob)
                    continue
                doctest_blocks.append(block)
        return doctest_blocks

    def __text__(self):
        return self.text

    def __eq__(self, other):
        if self is other:
            return True
        if not isinstance(other, PythonBlock):
            return NotImplemented
        return self.text == other.text and self.flags == other.flags

    def __ne__(self, other):
        if not isinstance(other, PythonBlock):
            return NotImplemented
        return not (self == other)

    def __cmp__(self, other):
        if self is other:
            return 0
        if not isinstance(other, PythonBlock):
            return NotImplemented
        return cmp(self.text, other.text) or cmp(self.flags, other.flags)

    def __hash__(self):
        h = hash((self.text, self.flags))
        self.__hash__ = lambda: h
        return h