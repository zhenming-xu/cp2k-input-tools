#!/usr/bin/env python3
# coding: utf-8

import re
import collections
import xml.etree.ElementTree as ET

from .tokenizer import tokenize, Context, TokenizerError
from .lineiterator import MultiFileLineIterator
from .keyword_helpers import parse_keyword
from .parser_errors import *


def _find_node_by_name(parent, tag, name):
    """check all specified nodes for matching names or aliases in the NAME tag"""

    for node in parent.iterfind(f"./{tag}"):
        if name.upper() in [e.text for e in node.iterfind("./NAME")]:
            return node

    return None


_Variable = collections.namedtuple("Variable", ["value", "ctx"])
_ConditionalBlock = collections.namedtuple("ConditionalBlock", ["condition", "ctx"])


_SECTION_MATCH = re.compile(r"&(?P<name>[\w\-_]+)\s*(?P<param>.*)")
_KEYWORD_MATCH = re.compile(r"(?P<name>[\w\-_]+)\s*(?P<value>.*)")

_CONDITIONAL_MATCH = re.compile(
    r"\s*@(?P<stmt>IF|ENDIF)\s*(?P<cond>.*)", flags=re.IGNORECASE
)
_SET_MATCH = re.compile(r"\s*@SET\s+(?P<var>\w+)\s+(?P<value>.+)", flags=re.IGNORECASE)
_INCLUDE_MATCH = re.compile(
    r"\s*@INCLUDE\s+(?P<file>('[^']+')|(\"[^']+\")|[^'\"].*)", flags=re.IGNORECASE
)


class CP2KInputParser:
    def __init__(self, xmlspec):
        # schema:
        self._parse_tree = ET.parse(xmlspec)
        self._nodes = [self._parse_tree.getroot()]

        # datatree being generated:
        self._tree = {}
        self._treerefs = [self._tree]

        # file handling:
        self._lineiter = MultiFileLineIterator()

        # preprocessor state:
        self._varstack = {}
        self._conditional_block = None

    def _parse_as_section(self, entry):
        match = _SECTION_MATCH.match(entry.line)

        section_name = match.group("name").upper()
        section_param = match.group("param")

        if section_name == "END":
            section_param = section_param.rstrip()

            if section_param and section_param.upper() not in [
                e.text for e in self._nodes[-1].iterfind("./NAME")
            ]:
                raise SectionMismatchError(
                    "could not match open section with name:", section_param
                )

            # if the END param was a match or none was specified, go a level up
            self._nodes.pop()
            self._treerefs.pop()
            return

        # check all section nodes for matching names or aliases
        section_node = _find_node_by_name(self._nodes[-1], "SECTION", section_name)

        if not section_node:
            raise ParserError(f"invalid section '{section_name}'")

        self._nodes += [
            section_node
        ]  # add the current XML section node to the stack of nodes
        repeats = True if section_node.get("repeats") == "yes" else False

        # CP2K uses the same names for keywords and sections (in the same section), prefix sections
        # using the '+' allows for unquoted section names in YAML
        section_name = f"+{section_name}"

        if section_name not in self._treerefs[-1]:
            # if we encounter this section the first time, simply add it
            self._treerefs[-1][section_name] = {}
            self._treerefs += [self._treerefs[-1][section_name]]

        elif repeats:
            # if we already have it AND it is in fact a repeating section
            if isinstance(self._treerefs[-1][section_name], list):
                # if the entry is already a list, then simply add a new empty dict for this section
                self._treerefs[-1][section_name] += [{}]
            else:
                # if the entry is not yet a list, convert it to one
                self._treerefs[-1][section_name] = [
                    self._treerefs[-1][section_name],
                    {},
                ]

            # the next entry in the stack shall be our newly created section
            self._treerefs += [self._treerefs[-1][section_name][-1]]

        else:
            raise InvalidNameError(
                f"the section '{section_name}' can not be defined multiple times:",
                section_token,
            )

        # check whether we got a parameter for the section and validate it
        param_node = section_node.find("./SECTION_PARAMETERS")
        if param_node:  # validate the section parameter like a kw datatype
            # there is no way we get a second section parameter, assign directly
            self._treerefs[-1]["_"] = parse_keyword(param_node, section_param).values
        elif section_param:
            raise ParserError("section parameters given for non-parametrized section")

    def _parse_as_keyword(self, entry):
        match = _KEYWORD_MATCH.match(entry.line)

        kw_name = match.group("name").upper()
        kw_value = match.group("value")

        kw_node = _find_node_by_name(self._nodes[-1], "KEYWORD", kw_name)

        # if no keyword with the given name has been found, check for a default keyword for this section
        if not kw_node:
            kw_node = _find_node_by_name(
                self._nodes[-1], "DEFAULT_KEYWORD", "DEFAULT_KEYWORD"
            )
            if kw_node:  # for default keywords, the whole line is the value
                kw_value = entry.line

        if not kw_node:
            raise InvalidNameError(
                "invalid keyword specified and no default keyword for this section"
            )

        kw = parse_keyword(kw_node, kw_value)

        if kw.name not in self._treerefs[-1]:
            # even if it is a repeating element, store it as a single value first
            self._treerefs[-1][kw.name] = kw.values

        elif kw.repeats:  # if the keyword already exists and is a repeating element
            if isinstance(self._treerefs[-1][kw.name], list):
                # ... and is already a list, simply append
                self._treerefs[-1][kw.name] += [kw.values]
            else:
                # ... otherwise turn it into a list now
                self._treerefs[-1][kw.name] = [self._treerefs[-1][kw.name], kw.values]

        else:
            # TODO: improve error message
            raise NameRepetitionError(
                f"the keyword '{kw.name}' can only be mentioned once"
            )

    def _resolve_variables(self, line):
        var_start = 0
        var_end = 0

        ctx = Context(line=line)

        # the following algorithm is from CP2Ks cp_parser_inpp_methods.F to reproduce its behavior :(

        # first replace all "${...}"  with no nesting, meaning that ${foo${bar}} means foo$bar is the key
        while True:
            var_start = line.find("${", var_end)
            if var_start < 0:
                break

            var_end = line.find("}", var_start + 2)
            if var_end < 0:
                ctx["colnr"] = len(line) - 1
                ctx["ref_colnr"] = var_start
                raise PreprocessorError(f"unterminated variable", ctx)

            key = line[var_start + 2 : var_end]  # without ${ and }
            try:
                value = self._varstack[key.upper()].value
            except KeyError:
                ctx["colnr"] = var_start
                ctx["ref_colnr"] = var_end
                raise PreprocessorError(f"undefined variable '{key}'", ctx) from None

            line = f"{line[:var_start]}{value}{line[var_end+1:]}"

        var_start = 0
        var_end = 0

        while True:
            var_start = line.find("$", var_end)
            if var_start < 0:
                break

            var_end = line.find(" ", var_start + 1)
            if var_end < 0:
                # -1 would be the last entry, but in a range it is without the specified entry
                var_end = len(line.rstrip())

            key = line[var_start + 1 : var_end]
            try:
                value = self._varstack[key.upper()].value
            except KeyError:
                ctx["colnr"] = var_start
                ctx["ref_colnr"] = var_end - 1
                raise PreprocessorError(f"undefined variable '{key}'", ctx) from None

            line = f"{line[:var_start]}{value}{line[var_end+1:]}"

        return line

    def _parse_preprocessor_instruction(self, line):
        conditional_match = _CONDITIONAL_MATCH.match(line)

        ctx = Context(line=line)

        if conditional_match:
            stmt = conditional_match.group("stmt")
            condition = conditional_match.group("cond").strip()

            if stmt.upper() == "ENDIF":
                if self._conditional_block is None:
                    raise PreprocessorError("found @ENDIF without a previous @IF", ctx)

                # check for garbage which is not a comment, note: we're stricter than CP2K here
                if condition and not condition.startswith("!"):
                    ctx["colnr"] = conditional_match.start("cond")
                    ctx["ref_colnr"] = conditional_match.end("cond")
                    raise PreprocessorError("garbage found after @ENDIF", ctx)

                self._conditional_block = None
            else:
                if self._conditional_block is not None:
                    ctx["ref_line"] = self._conditional_block.ctx["line"]
                    raise PreprocessorError("nested @IF are not allowed", ctx)

                # resolve any variables inside the condition
                try:
                    condition = self._resolve_variables(condition)
                except PreprocessorError as exc:
                    exc.args[1]["colnr"] += conditional_match.start("cond")
                    exc.args[1]["ref_colnr"] += conditional_match.start("cond")
                    raise

                # prefix-whitespace are consumed in the regex, suffix with the strip() above
                if not condition or condition == "0":
                    self._conditional_block = ConditionalBlock(False, ctx)
                elif "==" in condition:
                    lhs, rhs = [s.strip() for s in condition.split("==", maxsplit=1)]
                    self._conditional_block = ConditionalBlock(lhs == rhs, ctx)
                elif "/=" in condition:
                    lhs, rhs = [s.strip() for s in condition.split("/=", maxsplit=1)]
                    self._conditional_block = ConditionalBlock(lhs != rhs, ctx)
                else:
                    self._conditional_block = ConditionalBlock(True, ctx)

            return

        if self._conditional_block and not self._conditional_block.condition:
            return

        set_match = _SET_MATCH.match(line)
        if set_match:
            # resolve other variables in the definition first
            value = self._resolve_variables(set_match.group("value"))
            self._varstack[set_match.group("var").upper()] = _Variable(value, ctx)
            return

        include_match = _INCLUDE_MATCH.match(line)
        if include_match:
            # resolve variables first
            try:
                filename = self._resolve_variables(include_match.group("file"))
            except PreprocessorError as exc:
                exc.args[1]["colnr"] += include_match.start("file")
                exc.args[1]["ref_colnr"] += include_match.start("file")
                raise

            fhandle = open(filename.strip("'\""), "r")
            self._lineiter.add_file(fhandle)

            return

        raise PreprocessorError(f"unknown preprocessor directive found", ctx)

    def parse(self, fhandle):
        self._lineiter.add_file(fhandle)

        try:
            for entry in self._lineiter.lines():
                # ignore all comments:
                if entry.line.startswith(("!", "#")):
                    continue

                if entry.line.startswith("@"):
                    self._parse_preprocessor_instruction(entry.line)
                    continue

                # ignore everything in a disable @IF/@ENDIF block
                if self._conditional_block and not self._conditional_block.condition:
                    continue

                entry = entry._replace(line=self._resolve_variables(entry.line))

                if entry.line.startswith("&"):
                    self._parse_as_section(entry)
                    continue

                self._parse_as_keyword(entry)

            if self._conditional_block is not None:
                raise PreprocessorError(
                    f"conditional block not closed at end of file",
                    Context(ref_line=self._conditional_block.ctx["line"]),
                )

        except (PreprocessorError, TokenizerError) as exc:
            exc.args[1]["filename"] = fhandle.name
            exc.args[1]["linenr"] = linenr
            exc.args[1]["line"] = line
            raise

        return self._tree