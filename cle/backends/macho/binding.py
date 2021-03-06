# -*-coding:utf8 -*-
# This file is part of Mach-O Loader for CLE.
# Contributed December 2016 by Fraunhofer SIT (https://www.sit.fraunhofer.de/en/) and updated in September 2019.

import struct

from .symbol import BindingSymbol

from ...errors import CLEInvalidBinaryError
from ...address_translator import AT
from macholib import mach_o

import logging
l = logging.getLogger('cle.backends.macho.binding')

if bytes is not str:
    chh = lambda x: x
else:
    chh = ord

def read_uleb(blob, offset):
    """Reads a number encoded as uleb128"""
    result = 0
    shift = 0
    index = offset

    while index < len(blob):
        b = chh(blob[index])
        result |= ((b & 0x7f) << shift)
        shift += 7
        index += 1
        if b & 0x80 == 0:
            break

    return result, index - offset

def read_sleb(blob, offset):
    """Reads a number encoded as sleb128"""
    result = 0
    shift = 0
    index = offset

    while index < len(blob):
        b = chh(blob[index])
        result |= ((b & 0x7f) << shift)
        shift += 7
        index += 1
        if b & 0x80 == 0:
            if b & 0x40:
                # two's complement
                result -= (1 << shift)
            break

    return result, index - offset

class BindingState(object):
    """State object"""

    def __init__(self, is_64):
        self.index = 0
        self.done = False
        self.lib_ord = 0
        self.sym_name = ""
        self.sym_flags = 0
        self.binding_type = 0
        self.addend = 0
        self.segment_index = 0
        self.address = 0
        self.seg_end_address = 0  # TODO: no rebasing support
        self.wraparound = 2 ** 64  # address is expected to properly overflow and address is uintptr_t (unsigned long according to _uintptr_t.h)
        self.sizeof_intptr_t = 8 if is_64 else 4  # experimentally determined
        self.bind_handler = None  # function(state,binary) => None

    def add_address_ov(self, address, addend):
        """ this is a very ugly klugde. It is needed because dyld relies on overflow
            semantics and represents several negative offsets through BIG ulebs"""
        tmp = address + addend
        if tmp > self.wraparound:
            tmp -= self.wraparound
        self.address = tmp

    def check_address_bounds(self):
        if self.address >= self.seg_end_address:
            l.error("index %d: address >= seg_end_address (%#x >= %#x)", self.index, self.address, self.seg_end_address)
            raise CLEInvalidBinaryError()

class BindingHelper(object):
    """Factors out binding logic from MachO.
    Intended to work in close conjunction with MachO not for standalone use"""

    def __init__(self, binary):
        self.binary = binary

    def do_normal_bind(self, blob):
        """Performs non-lazy, non-weak bindings
        :param blob: Blob containing binding opcodes"""

        if blob is None:
            return  # skip

        l.debug("Binding non-lazy, non-weak symbols")
        s = BindingState(self.binary.arch.bits == 64)
        seg = self.binary.segments[0]
        # XXX: do self.binary.__text
        # is it possible to have more than one __text?
        s.seg_end_address = seg.vaddr + seg.memsize
        s.bind_handler = default_binding_handler
        self._do_bind_generic(blob, s, {
            mach_o.BIND_OPCODE_DONE: n_opcode_done,
            mach_o.BIND_OPCODE_SET_DYLIB_ORDINAL_IMM: n_opcode_set_dylib_ordinal_imm,
            mach_o.BIND_OPCODE_SET_DYLIB_ORDINAL_ULEB: n_opcode_set_dylib_ordinal_uleb,
            mach_o.BIND_OPCODE_SET_DYLIB_SPECIAL_IMM: n_opcode_set_dylib_special_imm,
            mach_o.BIND_OPCODE_SET_SYMBOL_TRAILING_FLAGS_IMM: n_opcode_set_trailing_flags_imm,
            mach_o.BIND_OPCODE_SET_TYPE_IMM: n_opcode_set_type_imm,
            mach_o.BIND_OPCODE_SET_ADDEND_SLEB: n_opcode_set_addend_sleb,
            mach_o.BIND_OPCODE_SET_SEGMENT_AND_OFFSET_ULEB: n_opcode_set_segment_and_offset_uleb,
            mach_o.BIND_OPCODE_ADD_ADDR_ULEB: n_opcode_add_addr_uleb,
            mach_o.BIND_OPCODE_DO_BIND: n_opcode_do_bind,
            mach_o.BIND_OPCODE_DO_BIND_ADD_ADDR_ULEB: n_opcode_do_bind_add_addr_uleb,
            mach_o.BIND_OPCODE_DO_BIND_ADD_ADDR_IMM_SCALED: n_opcode_do_bind_add_addr_imm_scaled,
            mach_o.BIND_OPCODE_DO_BIND_ULEB_TIMES_SKIPPING_ULEB: n_opcode_do_bind_uleb_times_skipping_uleb
        })

        l.debug("Done binding non-lazy, non-weak symbols ")

    def do_lazy_bind(self, blob):
        """
        Performs lazy binding
        """
        if blob is None:
            return  # skip
        l.debug("Binding lazy symbols")

        s = BindingState(self.binary.arch.bits == 64)
        s.index = 0
        s.bind_handler = default_binding_handler
        end = len(blob)
        # We need to iterate the iteration as every lazy binding entry ends with BIND_OPCODE_DONE
        while s.index < end:
            # re-initialise state (except index)
            s.binding_type = 1
            s.address = 0
            s.sym_name = ""
            s.sym_flags = 0
            s.lib_ord = 0
            s.done = False
            s.addend = 0
            s.segment_index = 0
            s.seg_end_address = 0  # TODO: no rebasing support

            self._do_bind_generic(blob, s, {
                mach_o.BIND_OPCODE_DONE: n_opcode_done,
                mach_o.BIND_OPCODE_SET_DYLIB_ORDINAL_IMM: n_opcode_set_dylib_ordinal_imm,
                mach_o.BIND_OPCODE_SET_DYLIB_ORDINAL_ULEB: n_opcode_set_dylib_ordinal_uleb,
                mach_o.BIND_OPCODE_SET_DYLIB_SPECIAL_IMM: n_opcode_set_dylib_special_imm,
                mach_o.BIND_OPCODE_SET_SYMBOL_TRAILING_FLAGS_IMM: n_opcode_set_trailing_flags_imm,
                mach_o.BIND_OPCODE_SET_TYPE_IMM: n_opcode_set_type_imm,
                mach_o.BIND_OPCODE_SET_SEGMENT_AND_OFFSET_ULEB: l_opcode_set_segment_and_offset_uleb,
                mach_o.BIND_OPCODE_DO_BIND: l_opcode_do_bind,
            })

        l.debug("Done binding lazy symbols")

    def _do_bind_generic(self, blob, init_state, opcode_dict):
        """
        Does the actual binding work. Represents a generic framework for interpreting binding opcodes
        :param blob: blob of binding opcodes
        :param init_state: Initial BindingState
        :param opcode_dict: Dictionary opcode=> handler
        :return: resulting binding state
        """

        s = init_state
        seg = self.binary.segments[s.segment_index]
        s.seg_end_address = seg.vaddr + seg.memsize  # TODO: no rebasing support
        end = len(blob)
        while not s.done and s.index < end:
            l.debug("Current address: %#x, blob index (offset): %#x", s.address, s.index)
            raw_opcode = blob[s.index]
            opcode = raw_opcode & mach_o.BIND_OPCODE_MASK
            immediate = raw_opcode & mach_o.BIND_IMMEDIATE_MASK
            s.index += 1
            try:
                binding_handler = opcode_dict[opcode]
                s = binding_handler(s, self.binary, immediate, blob)
            except KeyError:
                l.error("Invalid opcode for current binding: %#x", opcode)

        return s

# pylint: disable=unused-argument
# The following functions realize different variants of handling binding opcodes
# the format is def X(state,binary,immediate,blob) => state
def n_opcode_done(s, b, i, blob):
    l.debug("BIND_OPCODE_DONE @ %#x", s.index)
    s.done = True
    return s

def n_opcode_set_dylib_ordinal_imm(s, b, i, blob):
    l.debug("SET_DYLIB_ORDINAL_IMM @ %#x: %d", s.index, i)
    s.lib_ord = i
    return s

def n_opcode_set_dylib_ordinal_uleb(s, b, i, blob):
    uleb = read_uleb(blob, s.index)
    s.lib_ord = uleb[0]
    s.index += uleb[1]
    l.debug("SET_DYLIB_ORDINAL_ULEB @ %#x: %d", s.index, s.lib_ord)
    return s

def n_opcode_set_dylib_special_imm(s, b, i, blob):
    if i == 0:
        s.lib_ord = 0
    else:
        s.lib_ord = (i | mach_o.BIND_OPCODE_MASK) - 256
    l.debug("SET_DYLIB_SPECIAL_IMM @ %#x: %d", s.index, s.lib_ord)
    return s

def n_opcode_set_trailing_flags_imm(s, b, i, blob):
    s.sym_name = ""
    s.sym_flags = i

    while blob[s.index] != 0:
        s.sym_name += chr(blob[s.index])
        s.index += 1

    s.index += 1  # move past 0 byte
    l.debug("SET_SYMBOL_TRAILING_FLAGS_IMM @ %#x: %r,%#x", s.index - len(s.sym_name) - 1, s.sym_name, s.sym_flags)
    return s

def n_opcode_set_type_imm(s, b, i, blob):
    s.binding_type = i
    l.debug("SET_TYPE_IMM @ %#x: %d", s.index, s.binding_type)
    return s

def n_opcode_set_addend_sleb(s, b, i, blob):
    sleb = read_sleb(blob, s.index)
    s.addend = sleb[0]
    l.debug("SET_ADDEND_SLEB @ %#x: %d", s.index, s.addend)
    s.index += sleb[1]
    return s

def n_opcode_set_segment_and_offset_uleb(s, b, i, blob):
    s.segment_index = i
    uleb = read_uleb(blob, s.index)
    l.debug("(n)SET_SEGMENT_AND_OFFSET_ULEB @ %#x: %d, %d", s.index, s.segment_index, uleb[0])
    s.index += uleb[1]
    seg = b.segments[s.segment_index]
    s.add_address_ov(seg.vaddr, uleb[0])
    s.seg_end_address = seg.vaddr + seg.memsize

    return s

def l_opcode_set_segment_and_offset_uleb(s, b, i, blob):
    uleb = read_uleb(blob, s.index)
    l.debug("(l)SET_SEGMENT_AND_OFFSET_ULEB @ %#x: %d, %d", s.index, i, uleb[0])
    seg = b.segments[i]
    s.add_address_ov(seg.vaddr, uleb[0])
    s.index += uleb[1]
    return s

def n_opcode_add_addr_uleb(s, b, i, blob):
    uleb = read_uleb(blob, s.index)
    s.add_address_ov(s.address, uleb[0])
    l.debug("ADD_ADDR_ULEB @ %#x: %d", s.index, uleb[0])
    s.index += uleb[1]
    return s

def n_opcode_do_bind(s, b, i, blob):
    l.debug("(n)DO_BIND @ %#x", s.index)
    s.check_address_bounds()
    s.bind_handler(s, b)
    s.add_address_ov(s.address, s.sizeof_intptr_t)
    return s

def l_opcode_do_bind(s, b, i, blob):
    l.debug("(l)DO_BIND @ %#x", s.index)
    s.bind_handler(s, b)
    return s

def n_opcode_do_bind_add_addr_uleb(s, b, i, blob):
    uleb = read_uleb(blob, s.index)
    l.debug("DO_BIND_ADD_ADDR_ULEB @ %#x: %d", s.index, uleb[0])
    if s.address >= s.seg_end_address:
        l.error("DO_BIND_ADD_ADDR_ULEB @ %#x: address >= seg_end_address (%#x>=%#x)",
                s.index, s.address, s.seg_end_address)
        raise CLEInvalidBinaryError()
    s.index += uleb[1]
    s.bind_handler(s, b)
    # this is done AFTER binding in preparation for the NEXT step
    s.add_address_ov(s.address, uleb[0] + s.sizeof_intptr_t)
    return s

def n_opcode_do_bind_add_addr_imm_scaled(s, b, i, blob):
    l.debug("DO_BIND_ADD_ADDR_IMM_SCALED @ %#x: %d", s.index, i)
    if s.address >= s.seg_end_address:
        l.error("DO_BIND_ADD_ADDR_IMM_SCALED @ %#x: address >= seg_end_address (%#x>=%#x)",
                s.index, s.address, s.seg_end_address)
        raise CLEInvalidBinaryError()
    s.bind_handler(s, b)
    # this is done AFTER binding in preparation for the NEXT step
    s.add_address_ov(s.address, (i * s.sizeof_intptr_t) + s.sizeof_intptr_t)
    return s

def n_opcode_do_bind_uleb_times_skipping_uleb(s, b, i, blob):
    count = read_uleb(blob, s.index)
    s.index += count[1]
    skip = read_uleb(blob, s.index)
    s.index += skip[1]
    l.debug(
        "DO_BIND_ULEB_TIMES_SKIPPING_ULEB @ %#x: %d,%d", s.index - skip[1] - count[1], count[0], skip[0])
    for i in range(0, count[0]):
        if s.address >= s.seg_end_address:
            l.error("DO_BIND_ADD_ADDR_IMM_SCALED @ %#x: address >= seg_end_address (%#x >= %#x)",
                s.index - skip[1] - count[1], s.address, s.seg_end_address)
            raise CLEInvalidBinaryError()
        s.bind_handler(s, b)
        s.add_address_ov(s.address, skip[0] + s.sizeof_intptr_t)
    return s

# default binding handler
def default_binding_handler(state, binary):
    """Binds location to the symbol with the given name and library ordinal
    """

    # locate the symbol:
    # TODO: A lookup structure of some kind would be nice (see __init__)
    matches = list(
        filter(
            lambda s, compare_state=state:
                s.name == compare_state.sym_name and
                s.library_ordinal == compare_state.lib_ord
                and not s.is_stab, binary.symbols
        )
    )
    if len(matches) > 1:
        l.error("Cannot bind: More than one match for (%r,%d)", state.sym_name, state.lib_ord)
        raise CLEInvalidBinaryError()
    elif len(matches) < 1:
        l.info("No match for (%r,%d), generating BindingSymbol ...", state.sym_name, state.lib_ord)
        matches = [BindingSymbol(binary,state.sym_name,state.lib_ord)]
        binary.symbols.add(matches[0])
        binary._ordered_symbols.append(matches[0])

    symbol = matches[0]
    location = state.address

    # If the linked_addr is equal to zero, it's an imported symbol which is by that time unresolved.
    # Don't write addend's there

    value = symbol.linked_addr + state.addend if symbol.linked_addr != 0 else 0x0

    if state.binding_type == 1:  # POINTER
        l.debug("Updating address %#x with symobl %r @ %#x", location, state.sym_name, value)
        binary.memory.store(
            AT.from_lva(location, binary).to_rva(),
            struct.pack(binary.struct_byteorder + ("Q" if binary.arch.bits == 64 else "I"), value))
        symbol.bind_xrefs.append(location)
    elif state.binding_type == 2:  # ABSOLUTE32
        location_32 = location % (2 ** 32)
        value_32 = value % (2 ** 32)
        l.debug("Updating address %#x with symobl %r @ %#x", state.sym_name, location_32, value_32)
        binary.memory.store(
            AT.from_lva(location_32, binary).to_rva(),
            struct.pack(binary.struct_byteorder + "I", value_32))
        symbol.bind_xrefs.append(location_32)
    elif state.binding_type == 3:  # PCREL32
        location_32 = location % (2 ** 32)
        value_32 = (value - (location + 4)) % (2 ** 32)
        l.debug("Updating address %#x with symobl %r @ %#x", state.sym_name, location_32, value_32)
        binary.memory.store(
            AT.from_lva(location_32, binary).to_rva(),
            struct.pack(binary.struct_byteorder + "I", value_32))
        symbol.bind_xrefs.append(location_32)
    else:
        l.error("Unknown BIND_TYPE: %d", state.binding_type)
        raise CLEInvalidBinaryError()

