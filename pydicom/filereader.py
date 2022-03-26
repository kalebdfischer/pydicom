# Copyright 2008-2018 pydicom authors. See LICENSE file for details.
"""Read a dicom media file"""


# Need zlib and io.BytesIO for deflate-compressed file
from io import BytesIO
import math
import os
from struct import (Struct, unpack)
import sys
from pathlib import Path
from typing import (
    BinaryIO, Union, Optional, List, Any, Callable, cast, MutableSequence,
    Iterator, Dict, Tuple
)
import traceback
import warnings
import zlib

from pydicom import config
from pydicom.charset import default_encoding, convert_encodings
from pydicom.config import logger
from pydicom.encaps import encapsulate
from pydicom.datadict import dictionary_VR
from pydicom.dataelem import (
    DataElement, RawDataElement, DataElement_from_raw, empty_value_for_VR
)
from pydicom.dataset import Dataset, FileDataset, FileMetaDataset
from pydicom.dicomdir import DicomDir
from pydicom.encaps import get_frame_offsets
from pydicom.errors import InvalidDicomError
from pydicom.filebase import DicomFile, DicomFileLike
from pydicom.fileutil import (
    read_undefined_length_value, path_from_pathlike, PathType, _unpack_tag
)
from pydicom.misc import size_in_bytes
from pydicom.pixel_data_handlers.numpy_handler import unpack_bits
from pydicom.sequence import Sequence
from pydicom.tag import (
    ItemTag, SequenceDelimiterTag, TupleTag, Tag, BaseTag, TagListType
)
import pydicom.uid
from pydicom.util.hexutil import bytes2hex
from pydicom.valuerep import extra_length_VRs


_FLOAT_PIXEL_DATA_TAGS = {0x7FE00008, 0x7FE00009, }
_UINT_PIXEL_DATA_TAGS = {0x7FE00010, }
_PIXEL_DATA_TAGS = _FLOAT_PIXEL_DATA_TAGS.union(_UINT_PIXEL_DATA_TAGS)

_JPEG_SOI_MARKER = b'\xFF\xD8'  # also JPEG-LS
_JPEG_EOI_MARKER = b'\xFF\xD9'  # also JPEG-LS
_JPEG2000_SOC_MARKER = b'\xFF\x4F'
_JPEG2000_EOC_MARKER = b'\xFF\xD9'
_START_MARKERS = {_JPEG_SOI_MARKER, _JPEG2000_SOC_MARKER}
_END_MARKERS = {_JPEG_EOI_MARKER, _JPEG2000_EOC_MARKER}


def data_element_generator(
    fp: BinaryIO,
    is_implicit_VR: bool,
    is_little_endian: bool,
    stop_when: Optional[Callable[[BaseTag, Optional[str], int], bool]] = None,
    defer_size: Optional[Union[int, str, float]] = None,
    encoding: Union[str, MutableSequence[str]] = default_encoding,
    specific_tags: Optional[List[BaseTag]] = None
) -> Iterator[Union[RawDataElement, DataElement]]:
    """Create a generator to efficiently return the raw data elements.

    .. note::

        This function is used internally - usually there is no need to call it
        from user code. To read data from a DICOM file, :func:`dcmread`
        shall be used instead.

    Parameters
    ----------
    fp : file-like
        The file-like to read from.
    is_implicit_VR : bool
        ``True`` if the data is encoded as implicit VR, ``False`` otherwise.
    is_little_endian : bool
        ``True`` if the data is encoded as little endian, ``False`` otherwise.
    stop_when : None, callable, optional
        If ``None`` (default), then the whole file is read. A callable which
        takes tag, VR, length, and returns ``True`` or ``False``. If it
        returns ``True``, ``read_data_element`` will just return.
    defer_size : int, str or float, optional
        See :func:`dcmread` for parameter info.
    encoding : Union[str, MutableSequence[str]]
        Encoding scheme
    specific_tags : list or None
        See :func:`dcmread` for parameter info.

    Yields
    -------
    RawDataElement or DataElement
        Yields DataElement for undefined length UN or SQ, RawDataElement
        otherwise.
    """
    # Summary of DICOM standard PS3.5-2008 chapter 7:
    # If Implicit VR, data element is:
    #    tag, 4-byte length, value.
    #        The 4-byte length can be FFFFFFFF (undefined length)*
    #
    # If Explicit VR:
    #    if OB, OW, OF, SQ, UN, or UT:
    #       tag, VR, 2-bytes reserved (both zero), 4-byte length, value
    #           For all but UT, the length can be FFFFFFFF (undefined length)*
    #   else: (any other VR)
    #       tag, VR, (2 byte length), value
    # * for undefined length, a Sequence Delimitation Item marks the end
    #        of the Value Field.
    # Note, except for the special_VRs, both impl and expl VR use 8 bytes;
    #    the special VRs follow the 8 bytes with a 4-byte length

    # With a generator, state is stored, so we can break down
    #    into the individual cases, and not have to check them again for each
    #    data element
    from pydicom.values import convert_string

    if is_little_endian:
        endian_chr = "<"
    else:
        endian_chr = ">"

    # assign implicit VR struct to variable as use later if VR assumed missing
    implicit_VR_struct = Struct(endian_chr + "HHL")
    if is_implicit_VR:
        element_struct = implicit_VR_struct
    else:  # Explicit VR
        # tag, VR, 2-byte length (or 0 if special VRs)
        element_struct = Struct(endian_chr + "HH2sH")
        extra_length_struct = Struct(endian_chr + "L")  # for special VRs
        extra_length_unpack = extra_length_struct.unpack  # for lookup speed

    # Make local variables so have faster lookup
    fp_read = fp.read
    fp_tell = fp.tell
    logger_debug = logger.debug
    debugging = config.debugging
    element_struct_unpack = element_struct.unpack
    defer_size = size_in_bytes(defer_size)

    tag_set = {Tag(tag) for tag in specific_tags} if specific_tags else set()
    has_tag_set = bool(tag_set)
    if has_tag_set:
        tag_set.add(Tag(0x00080005))  # Specific Character Set

    while True:
        # VR: Optional[str]

        # Read tag, VR, length, get ready to read value
        bytes_read = fp_read(8)
        if len(bytes_read) < 8:
            return  # at end of file

        if debugging:
            debug_msg = f"{fp.tell() - 8:08x}: {bytes2hex(bytes_read)}"

        if is_implicit_VR:
            # must reset VR each time; could have set last iteration (e.g. SQ)
            VR = None
            group, elem, length = element_struct_unpack(bytes_read)
        else:  # explicit VR
            group, elem, VR, length = element_struct_unpack(bytes_read)
            # defend against switching to implicit VR, some writer do in SQ's
            # issue 1067, issue 1035

            if not (b'AA' <= VR <= b'ZZ') and config.assume_implicit_vr_switch:
                # invalid VR, must be 2 cap chrs, assume implicit and continue
                VR = None
                group, elem, length = implicit_VR_struct.unpack(bytes_read)
            else:
                VR = VR.decode(default_encoding)
                if VR in extra_length_VRs:
                    bytes_read = fp_read(4)
                    length = extra_length_unpack(bytes_read)[0]
                    if debugging:
                        debug_msg += " " + bytes2hex(bytes_read)

        if debugging:
            debug_msg = "%-47s  (%04x, %04x)" % (debug_msg, group, elem)
            if not is_implicit_VR:
                debug_msg += " %s " % VR
            if length != 0xFFFFFFFF:
                debug_msg += "Length: %d" % length
            else:
                debug_msg += "Length: Undefined length (FFFFFFFF)"
            logger_debug(debug_msg)

        # Positioned to read the value, but may not want to -- check stop_when
        value_tell = fp_tell()
        tag = TupleTag((group, elem))
        if stop_when is not None:
            # XXX VR may be None here!! Should stop_when just take tag?
            if stop_when(tag, VR, length):
                if debugging:
                    logger_debug("Reading ended by stop_when callback. "
                                 "Rewinding to start of data element.")
                rewind_length = 8
                if not is_implicit_VR and VR in extra_length_VRs:
                    rewind_length += 4
                fp.seek(value_tell - rewind_length)
                return

        # Reading the value
        # First case (most common): reading a value with a defined length
        if length != 0xFFFFFFFF:
            # don't defer loading of Specific Character Set value as it is
            # needed immediately to get the character encoding for other tags
            if has_tag_set and tag not in tag_set:
                # skip the tag if not in specific tags
                fp.seek(fp_tell() + length)
                continue

            if (defer_size is not None and length > defer_size and
                    tag != BaseTag(0x00080005)):
                # Flag as deferred by setting value to None, and skip bytes
                value = None
                logger_debug("Defer size exceeded. "
                             "Skipping forward to next data element.")
                fp.seek(fp_tell() + length)
            else:
                value = (
                    fp_read(length) if length > 0
                    else cast(
                        Optional[bytes], empty_value_for_VR(VR, raw=True)
                    )
                )
                if debugging:
                    dotdot = "..." if length > 20 else "   "
                    displayed_value = value[:20] if value else b''
                    logger_debug("%08x: %-34s %s %r %s" %
                                 (value_tell, bytes2hex(displayed_value),
                                  dotdot, displayed_value, dotdot))

            # If the tag is (0008,0005) Specific Character Set, then store it
            if tag == BaseTag(0x00080005):
                # *Specific Character String* is b'' for empty value
                encoding = convert_string(
                    cast(bytes, value) or b'', is_little_endian
                )
                # Store the encoding value in the generator
                # for use with future elements (SQs)
                encoding = convert_encodings(encoding)

            yield RawDataElement(tag, VR, length, value, value_tell,
                                 is_implicit_VR, is_little_endian)

        # Second case: undefined length - must seek to delimiter,
        # unless is SQ type, in which case is easier to parse it, because
        # undefined length SQs and items of undefined lengths can be nested
        # and it would be error-prone to read to the correct outer delimiter
        else:
            # VR UN with undefined length shall be handled as SQ
            # see PS 3.5, section 6.2.2
            if VR == 'UN':
                VR = 'SQ'
            # Try to look up type to see if is a SQ
            # if private tag, won't be able to look it up in dictionary,
            #   in which case just ignore it and read the bytes unless it is
            #   identified as a Sequence
            if VR is None or VR == 'UN' and config.replace_un_with_known_vr:
                try:
                    VR = dictionary_VR(tag)
                except KeyError:
                    # Look ahead to see if it consists of items
                    # and is thus a SQ
                    next_tag = _unpack_tag(fp_read(4), endian_chr)
                    # Rewind the file
                    fp.seek(fp_tell() - 4)
                    if next_tag == ItemTag:
                        VR = 'SQ'

            if VR == 'SQ':
                if debugging:
                    logger_debug(
                        f"{fp_tell():08X}: Reading/parsing undefined length "
                        "sequence"
                    )

                seq = read_sequence(fp, is_implicit_VR,
                                    is_little_endian, length, encoding)
                if has_tag_set and tag not in tag_set:
                    continue

                yield DataElement(tag, VR, seq, value_tell,
                                  is_undefined_length=True)
            else:
                delimiter = SequenceDelimiterTag
                if debugging:
                    logger_debug("Reading undefined length data element")
                value = read_undefined_length_value(
                    fp, is_little_endian, delimiter, defer_size
                )

                # tags with undefined length are skipped after read
                if has_tag_set and tag not in tag_set:
                    continue

                yield RawDataElement(tag, VR, length, value, value_tell,
                                     is_implicit_VR, is_little_endian)


def _is_implicit_vr(
    fp: BinaryIO,
    implicit_vr_is_assumed: bool,
    is_little_endian: bool,
    stop_when: Optional[Callable[[BaseTag, Optional[str], int], bool]],
    is_sequence: bool
) -> bool:
    """Check if the real VR is explicit or implicit.

    Parameters
    ----------
    fp : an opened file object
    implicit_vr_is_assumed : bool
        True if implicit VR is assumed.
        If this does not match with the real transfer syntax, a user warning
        will be issued.
    is_little_endian : bool
        True if file has little endian transfer syntax.
        Needed to interpret the first tag.
    stop_when : None, optional
        Optional call_back function which can terminate reading.
        Needed to check if the next tag still belongs to the read dataset.
    is_sequence : bool
        True if called for a sequence, False for a top-level dataset.

    Returns
    -------
    True if implicit VR is used, False otherwise.
    """
    # sequences do not switch from implicit to explicit encoding,
    # but they are allowed to use implicit encoding if the dataset
    # is encoded as explicit VR
    if is_sequence and implicit_vr_is_assumed:
        return True

    tag_bytes = fp.read(4)
    raw_vr = fp.read(2)
    if len(raw_vr) < 2:
        return implicit_vr_is_assumed

    # it is sufficient to check if the VR is in valid ASCII range, as it is
    # extremely unlikely that the tag length accidentally has such a
    # representation - this would need the first tag to be longer than 16kB
    # (e.g. it should be > 0x4141 = 16705 bytes)
    found_implicit = not (0x40 < raw_vr[0] < 0x5B and 0x40 < raw_vr[1] < 0x5B)
    if found_implicit != implicit_vr_is_assumed:
        # first check if the tag still belongs to the dataset if stop_when
        # is given - if not, the dataset is empty and we just return
        endian_chr = "<" if is_little_endian else ">"
        tag = _unpack_tag(tag_bytes, endian_chr)
        vr = raw_vr.decode(default_encoding)
        if stop_when is not None and stop_when(tag, vr, 0):
            return found_implicit

        # sequences with undefined length can be encoded in implicit VR,
        # see PS 3.5, section 6.2.2
        if found_implicit and is_sequence:
            return True

        # got to the real problem - warn or raise depending on config
        found_vr = 'implicit' if found_implicit else 'explicit'
        expected_vr = 'implicit' if not found_implicit else 'explicit'
        msg = f"Expected {expected_vr} VR, but found {found_vr} VR"
        if config.enforce_valid_values:
            raise InvalidDicomError(msg)

        warnings.warn(msg + f" - using {found_vr} VR for reading", UserWarning)

    return found_implicit


def read_dataset(
    fp: BinaryIO,
    is_implicit_VR: bool,
    is_little_endian: bool,
    bytelength: Optional[int] = None,
    stop_when: Optional[Callable[[BaseTag, Optional[str], int], bool]] = None,
    defer_size: Optional[Union[str, int, float]] = None,
    parent_encoding: Union[str, MutableSequence[str]] = default_encoding,
    specific_tags: Optional[List[BaseTag]] = None,
    at_top_level: bool = True
) -> Dataset:
    """Return a :class:`~pydicom.dataset.Dataset` instance containing the next
    dataset in the file.

    Parameters
    ----------
    fp : file-like
        An opened file-like object.
    is_implicit_VR : bool
        ``True`` if file transfer syntax is implicit VR.
    is_little_endian : bool
        ``True`` if file has little endian transfer syntax.
    bytelength : int, None, optional
        ``None`` to read until end of file or ItemDeliterTag, else a fixed
        number of bytes to read
    stop_when : None, optional
        Optional call_back function which can terminate reading. See help for
        :func:`data_element_generator` for details
    defer_size : int, str or float, optional
        Size to avoid loading large elements in memory. See :func:`dcmread` for
        more parameter info.
    parent_encoding : str or List[str]
        Optional encoding to use as a default in case (0008,0005) *Specific
        Character Set* isn't specified.
    specific_tags : list of BaseTag, optional
        See :func:`dcmread` for parameter info.
    at_top_level: bool
        If dataset is top level (not within a sequence).
        Used to turn off explicit VR heuristic within sequences

    Returns
    -------
    dataset.Dataset
        A Dataset instance.

    See Also
    --------
    :class:`~pydicom.dataset.Dataset`
        A collection (dictionary) of DICOM
        :class:`~pydicom.dataelem.DataElement` instances.
    """
    raw_data_elements: Dict[BaseTag, Union[RawDataElement, DataElement]] = {}
    fp_start = fp.tell()
    is_implicit_VR = _is_implicit_vr(
        fp, is_implicit_VR, is_little_endian, stop_when,
        is_sequence=not at_top_level
    )
    fp.seek(fp_start)
    de_gen = data_element_generator(
        fp,
        is_implicit_VR,
        is_little_endian,
        stop_when,
        defer_size,
        parent_encoding,
        specific_tags,
    )
    try:
        while (bytelength is None) or (fp.tell() - fp_start < bytelength):
            raw_data_element = next(de_gen)
            # Read data elements. Stop on some errors, but return what was read
            tag = raw_data_element.tag
            # Check for ItemDelimiterTag --dataset is an item in a sequence
            if tag == BaseTag(0xFFFEE00D):
                break
            raw_data_elements[tag] = raw_data_element
    except StopIteration:
        pass
    except EOFError as details:
        if config.enforce_valid_values:
            raise
        msg = str(details) + " in file " + getattr(fp, "name", "<no filename>")
        warnings.warn(msg, UserWarning)
    except NotImplementedError as details:
        logger.error(details)

    ds = Dataset(raw_data_elements)

    encoding: Union[str, MutableSequence[str]]
    if 0x00080005 in raw_data_elements:
        elem = cast(RawDataElement, raw_data_elements[BaseTag(0x00080005)])
        char_set = cast(
            Optional[Union[str, MutableSequence[str]]],
            DataElement_from_raw(elem).value
        )
        encoding = convert_encodings(char_set)  # -> List[str]
    else:
        encoding = parent_encoding  # -> Union[str, MutableSequence[str]]

    ds.set_original_encoding(is_implicit_VR, is_little_endian, encoding)
    return ds


def read_sequence(
    fp: BinaryIO,
    is_implicit_VR: bool,
    is_little_endian: bool,
    bytelength: int,
    encoding: Union[str, MutableSequence[str]],
    offset: int = 0
) -> Sequence:
    """Read and return a :class:`~pydicom.sequence.Sequence` -- i.e. a
    :class:`list` of :class:`Datasets<pydicom.dataset.Dataset>`.
    """
    seq = []  # use builtin list to start for speed, convert to Sequence at end
    is_undefined_length = False
    if bytelength != 0:  # SQ of length 0 possible (PS 3.5-2008 7.5.1a (p.40)
        if bytelength == 0xffffffff:
            is_undefined_length = True
            bytelength = 0

        fp_tell = fp.tell  # for speed in loop
        fpStart = fp_tell()
        while (not bytelength) or (fp_tell() - fpStart < bytelength):
            file_tell = fp.tell()
            dataset = read_sequence_item(
                fp, is_implicit_VR, is_little_endian, encoding, offset
            )
            if dataset is None:  # None is returned if hit Sequence Delimiter
                break

            dataset.file_tell = file_tell + offset
            seq.append(dataset)

    sequence = Sequence(seq)
    sequence.is_undefined_length = is_undefined_length
    return sequence


def read_sequence_item(
    fp: BinaryIO,
    is_implicit_VR: bool,
    is_little_endian: bool,
    encoding: Union[str, MutableSequence[str]],
    offset: int = 0
) -> Optional[Dataset]:
    """Read and return a single :class:`~pydicom.sequence.Sequence` item, i.e.
    a :class:`~pydicom.dataset.Dataset`.
    """
    seq_item_tell = fp.tell() + offset
    if is_little_endian:
        tag_length_format = "<HHL"
    else:
        tag_length_format = ">HHL"
    try:
        bytes_read = fp.read(8)
        group, element, length = unpack(tag_length_format, bytes_read)
    except BaseException:
        raise IOError(
            f"No tag to read at file position {fp.tell() + offset:X}"
        )

    tag = (group, element)
    if tag == SequenceDelimiterTag:  # No more items, time to stop reading
        logger.debug(
            f"{fp.tell() - 8 + offset:08x}: End of Sequence"
        )
        if length != 0:
            logger.warning(
                f"Expected 0x00000000 after delimiter, found 0x{length:X}, "
                f"at position 0x{fp.tell() - 4 + offset:X}"
            )
        return None

    if tag != ItemTag:
        logger.warning(
            f"Expected sequence item with tag {ItemTag} at file position "
            f"0x{fp.tell() - 4 + offset:X}"
        )
    else:
        logger.debug(
            f"{fp.tell() - 4 + offset:08x}: {bytes2hex(bytes_read)}  "
            "Found Item tag (start of item)"
        )

    if length == 0xFFFFFFFF:
        ds = read_dataset(fp, is_implicit_VR, is_little_endian,
                          bytelength=None, parent_encoding=encoding,
                          at_top_level=False)
        ds.is_undefined_length_sequence_item = True
    else:
        ds = read_dataset(fp, is_implicit_VR, is_little_endian, length,
                          parent_encoding=encoding,
                          at_top_level=False)
        ds.is_undefined_length_sequence_item = False

        logger.debug(f"{fp.tell() + offset:08X}: Finished sequence item")

    ds.seq_item_tell = seq_item_tell
    return ds


def _read_command_set_elements(fp: BinaryIO) -> Dataset:
    """Return a Dataset containing any Command Set (0000,eeee) elements
    in `fp`.

    Command Set elements are always Implicit VR Little Endian (DICOM Standard,
    Part 7, :dcm:`Section 6.3<part07/sect_6.3.html>`). Once any Command Set
    elements are read `fp` will be positioned at the start of the next group
    of elements.

    Parameters
    ----------
    fp : file-like
        The file-like positioned at the start of any command set elements.

    Returns
    -------
    dataset.Dataset
        The command set elements as a Dataset instance. May be empty if no
        command set elements are present.
    """

    def _not_group_0000(tag: BaseTag, VR: Optional[str], length: int) -> bool:
        """Return True if the tag is not in group 0x0000, False otherwise."""
        return tag.group != 0

    return read_dataset(
        fp,
        is_implicit_VR=True,
        is_little_endian=True,
        stop_when=_not_group_0000
    )


def _read_file_meta_info(fp: BinaryIO) -> FileMetaDataset:
    """Return a Dataset containing any File Meta (0002,eeee) elements in `fp`.

    File Meta elements are always Explicit VR Little Endian (DICOM Standard,
    Part 10, :dcm:`Section 7<part10/chapter_7.html>`). Once any File Meta
    elements are read `fp` will be positioned at the start of the next group
    of elements.

    Parameters
    ----------
    fp : file-like
        The file-like positioned at the start of any File Meta Information
        group elements.

    Returns
    -------
    dataset.Dataset
        The File Meta elements as a Dataset instance. May be empty if no
        File Meta are present.
    """

    def _not_group_0002(tag: BaseTag, VR: Optional[str], length: int) -> bool:
        """Return True if the tag is not in group 0x0002, False otherwise."""
        return tag.group != 2

    start_file_meta = fp.tell()
    file_meta = FileMetaDataset(
        read_dataset(
            fp, is_implicit_VR=False, is_little_endian=True,
            stop_when=_not_group_0002
        )
    )
    if not file_meta._dict:
        return file_meta

    # Test the file meta for correct interpretation by requesting the first
    #   data element: if it fails, retry loading the file meta with an
    #   implicit VR (issue #503)
    try:
        file_meta[list(file_meta.elements())[0].tag]
    except NotImplementedError:
        fp.seek(start_file_meta)
        file_meta = FileMetaDataset(
            read_dataset(
                fp, is_implicit_VR=True, is_little_endian=True,
                stop_when=_not_group_0002
            )
        )

    # Log if the Group Length doesn't match actual length
    if 'FileMetaInformationGroupLength' in file_meta:
        # FileMetaInformationGroupLength must be 12 bytes long and its value
        #   counts from the beginning of the next element to the end of the
        #   file meta elements
        actual_len = fp.tell() - (start_file_meta + 12)
        elem_len = file_meta.FileMetaInformationGroupLength
        if elem_len != actual_len:
            logger.info(
                "_read_file_meta_info: (0002,0000) 'File Meta Information "
                "Group Length' value doesn't match the actual File Meta "
                f"Information length ({elem_len} vs {actual_len} bytes)"
            )

    return file_meta


def read_file_meta_info(filename: PathType) -> FileMetaDataset:
    """Read and return the DICOM file meta information only.

    This function is meant to be used in user code, for quickly going through
    a series of files to find one which is referenced to a particular SOP,
    without having to read the entire files.
    """
    with open(filename, 'rb') as fp:
        read_preamble(fp, False)  # if no header, raise exception
        return _read_file_meta_info(fp)


def read_preamble(fp: BinaryIO, force: bool) -> Optional[bytes]:
    """Return the 128-byte DICOM preamble in `fp` if present.

    `fp` should be positioned at the start of the file-like. If the preamble
    and prefix are found then after reading `fp` will be positioned at the
    first byte after the prefix (byte offset 133). If either the preamble or
    prefix are missing and `force` is ``True`` then after reading `fp` will be
    positioned at the start of the file-like.

    Parameters
    ----------
    fp : file-like object
        The file-like to read the preamble from.
    force : bool
        Flag to force reading of a file even if no header is found.

    Returns
    -------
    preamble : bytes or None
        The 128-byte DICOM preamble will be returned if the appropriate prefix
        ('DICM') is found at byte offset 128. Returns ``None`` if the 'DICM'
        prefix is not found and `force` is ``True``.

    Raises
    ------
    InvalidDicomError
        If `force` is ``False`` and no appropriate header information found.

    Notes
    -----
    Also reads past the 'DICM' marker. Rewinds file to the beginning if
    no header found.
    """
    logger.debug("Reading File Meta Information preamble...")
    preamble = fp.read(128)
    if config.debugging:
        sample = bytes2hex(preamble[:8]) + "..." + bytes2hex(preamble[-8:])
        logger.debug(f"{fp.tell() - 128:08x}: {sample}")

    logger.debug("Reading File Meta Information prefix...")
    magic = fp.read(4)
    if magic != b"DICM" and force:
        logger.info(
            "File is not conformant with the DICOM File Format: 'DICM' "
            "prefix is missing from the File Meta Information header "
            "or the header itself is missing. Assuming no header and "
            "continuing."
        )
        fp.seek(0)
        return None

    if magic != b"DICM" and not force:
        raise InvalidDicomError(
            "File is missing DICOM File Meta Information header or the 'DICM' "
            "prefix is missing from the header. Use force=True to force "
            "reading."
        )
    else:
        logger.debug(f"{fp.tell() - 4:08x}: 'DICM' prefix found")

    return preamble


def _at_pixel_data(tag: BaseTag, VR: Optional[str], length: int) -> bool:
    pixel_data_tags = {0x7fe00010, 0x7fe00009, 0x7fe00008}
    return tag in pixel_data_tags


def read_partial(
    fileobj: BinaryIO,
    stop_when: Optional[Callable[[BaseTag, Optional[str], int], bool]] = None,
    defer_size: Optional[Union[int, str, float]] = None,
    force: bool = False,
    specific_tags: Optional[List[BaseTag]] = None
) -> Union[FileDataset, DicomDir]:
    """Parse a DICOM file until a condition is met.

    Parameters
    ----------
    fileobj : a file-like object
        Note that the file will not close when the function returns.
    stop_when :
        Stop condition. See :func:`read_dataset` for more info.
    defer_size : int, str or float, optional
        See :func:`dcmread` for parameter info.
    force : bool
        See :func:`dcmread` for parameter info.
    specific_tags : list or None
        See :func:`dcmread` for parameter info.

    Notes
    -----
    Use :func:`dcmread` unless you need to stop on some condition other than
    reaching pixel data.

    Returns
    -------
    dataset.FileDataset or dicomdir.DicomDir
        The read dataset.

    See Also
    --------
    dcmread
        More generic file reading function.
    """
    # Read File Meta Information

    # Read preamble (if present)
    preamble = read_preamble(fileobj, force)
    # Read any File Meta Information group (0002,eeee) elements (if present)
    file_meta_dataset = _read_file_meta_info(fileobj)

    # Read Dataset

    # Read any Command Set group (0000,eeee) elements (if present)
    command_set = _read_command_set_elements(fileobj)

    # Check to see if there's anything left to read
    peek = fileobj.read(1)
    if peek != b'':
        fileobj.seek(-1, 1)

    # `filobj` should be positioned at the start of the dataset by this point.
    # Ensure we have appropriate values for `is_implicit_VR` and
    # `is_little_endian` before we try decoding. We assume an initial
    # transfer syntax of implicit VR little endian and correct it as necessary
    is_implicit_VR = True
    is_little_endian = True
    transfer_syntax = file_meta_dataset.get("TransferSyntaxUID")
    if peek == b'':  # EOF
        pass
    elif transfer_syntax is None:  # issue 258
        # If no TransferSyntaxUID element then we have to try and figure out
        #   the correct values for `is_little_endian` and `is_implicit_VR`.
        # Peek at the first 6 bytes to get the first element's tag group and
        #   (possibly) VR
        group, _, VR = unpack("<HH2s", fileobj.read(6))
        fileobj.seek(-6, 1)

        # Test the VR to see if it's valid, and if so then assume explicit VR
        from pydicom.values import converters
        VR = VR.decode(default_encoding)
        if VR in converters.keys():
            is_implicit_VR = False
            # Big endian encoding can only be explicit VR
            #   Big endian 0x0004 decoded as little endian will be 1024
            #   Big endian 0x0100 decoded as little endian will be 1
            # Therefore works for big endian tag groups up to 0x00FF after
            #   which it will fail, in which case we leave it as little endian
            #   and hope for the best (big endian is retired anyway)
            if group >= 1024:
                is_little_endian = False
    elif transfer_syntax == pydicom.uid.ImplicitVRLittleEndian:
        pass
    elif transfer_syntax == pydicom.uid.ExplicitVRLittleEndian:
        is_implicit_VR = False
    elif transfer_syntax == pydicom.uid.ExplicitVRBigEndian:
        is_implicit_VR = False
        is_little_endian = False
    elif transfer_syntax == pydicom.uid.DeflatedExplicitVRLittleEndian:
        # See PS3.5 section A.5
        # when written, the entire dataset following
        #     the file metadata was prepared the normal way,
        #     then "deflate" compression applied.
        #  All that is needed here is to decompress and then
        #     use as normal in a file-like object
        zipped = fileobj.read()
        # -MAX_WBITS part is from comp.lang.python answer:
        # groups.google.com/group/comp.lang.python/msg/e95b3b38a71e6799
        unzipped = zlib.decompress(zipped, -zlib.MAX_WBITS)
        fileobj = BytesIO(unzipped)  # a file-like object
        is_implicit_VR = False
    else:
        # Any other syntax should be Explicit VR Little Endian,
        #   e.g. all Encapsulated (JPEG etc) are ExplVR-LE
        #        by Standard PS 3.5-2008 A.4 (p63)
        is_implicit_VR = False

    # Try and decode the dataset
    #   By this point we should be at the start of the dataset and have
    #   the transfer syntax (whether read from the file meta or guessed at)
    try:
        dataset = read_dataset(
            fileobj,
            is_implicit_VR,
            is_little_endian,
            stop_when=stop_when,
            defer_size=defer_size,
            specific_tags=specific_tags,
        )
    except EOFError:
        if config.enforce_valid_values:
            raise
        # warning already logged in read_dataset

    # Add the command set elements to the dataset (if any)
    dataset.update(command_set)

    class_uid = cast(
        pydicom.uid.UID, file_meta_dataset.get("MediaStorageSOPClassUID", None)
    )
    ds: Union[DicomDir, FileDataset]
    if class_uid and class_uid.name == "Media Storage Directory Storage":
        warnings.warn(
            "The 'DicomDir' class is deprecated and will be removed in v3.0, "
            "after which 'dcmread()' will return a normal 'FileDataset' "
            "instance for 'Media Storage Directory' SOP Instances.",
            DeprecationWarning
        )
        ds = DicomDir(
            fileobj,
            dataset,
            preamble,
            file_meta_dataset,
            is_implicit_VR,
            is_little_endian,
        )
    else:
        ds = FileDataset(
            fileobj,
            dataset,
            preamble,
            file_meta_dataset,
            is_implicit_VR,
            is_little_endian,
        )
    # save the originally read transfer syntax properties in the dataset
    ds.set_original_encoding(
        is_implicit_VR, is_little_endian, dataset._character_set
    )
    return ds


def dcmread(
    fp: Union[PathType, BinaryIO, DicomFileLike],
    defer_size: Optional[Union[str, int, float]] = None,
    stop_before_pixels: bool = False,
    force: bool = False,
    specific_tags: Optional[TagListType] = None
) -> Union[FileDataset, DicomDir]:
    """Read and parse a DICOM dataset stored in the DICOM File Format.

    Read a DICOM dataset stored in accordance with the :dcm:`DICOM File
    Format <part10/chapter_7.html>`. If the dataset is not stored in
    accordance with the File Format (i.e. the preamble and prefix are missing,
    there are missing required Type 1 *File Meta Information Group* elements
    or the entire *File Meta Information* is missing) then you will have to
    set `force` to ``True``.

    .. deprecated:: 2.2

        Returning a :class:`~pydicom.dicomdir.DicomDir` is deprecated and
        will be removed in v3.0. Use :class:`~pydicom.fileset.FileSet` instead.


    Examples
    --------
    Read and return a dataset stored in accordance with the DICOM File Format:

    >>> ds = pydicom.dcmread("CT_small.dcm")
    >>> ds.PatientName

    Read and return a dataset not in accordance with the DICOM File Format:

    >>> ds = pydicom.dcmread("rtplan.dcm", force=True)
    >>> ds.PatientName

    Use within a context manager:

    >>> with pydicom.dcmread("rtplan.dcm") as ds:
    ...     ds.PatientName

    Parameters
    ----------
    fp : str or PathLike or file-like
        Either a file-like object, a string containing the file name or the
        path to the file. The file-like object must have ``seek()``,
        ``read()`` and ``tell()`` methods and the caller is responsible for
        closing it (if required).
    defer_size : int, str or float, optional
        If not used then all elements are read into memory. If specified,
        then if a data element's stored value is larger than `defer_size`, the
        value is not read into memory until it is accessed in code. Should be
        the number of bytes to be read as :class:`int` or as a :class:`str`
        with units, e.g. ``'512 KB'``, ``'2 MB'``.
    stop_before_pixels : bool, optional
        If ``False`` (default), the full file will be read and parsed. Set
        ``True`` to stop before reading (7FE0,0010) *Pixel Data* (and all
        subsequent elements).
    force : bool, optional
        If ``False`` (default), raises an
        :class:`~pydicom.errors.InvalidDicomError` if the file is
        missing the *File Meta Information* header. Set to ``True`` to force
        reading even if no *File Meta Information* header is found.
    specific_tags : list of (int or str or 2-tuple of int), optional
        If used the only the supplied tags will be returned. The supplied
        elements can be tags or keywords. Note that the element (0008,0005)
        *Specific Character Set* is always returned if present - this ensures
        correct decoding of returned text values.

    Returns
    -------
    FileDataset or DicomDir
        An instance of :class:`~pydicom.dataset.FileDataset` that represents
        a parsed DICOM file, unless the dataset is a *Media Storage Directory*
        instance in which case it will be a
        :class:`~pydicom.dicomdir.DicomDir`.

    Raises
    ------
    InvalidDicomError
        If `force` is ``False`` and the file is not a valid DICOM file.
    TypeError
        If `fp` is ``None`` or of an unsupported type.

    See Also
    --------
    pydicom.dataset.FileDataset
        Data class that is returned.
    pydicom.filereader.read_partial
        Only read part of a DICOM file, stopping on given conditions.
    """
    # Open file if not already a file object
    caller_owns_file = True
    fp = path_from_pathlike(fp)
    if isinstance(fp, str):
        # caller provided a file name; we own the file handle
        caller_owns_file = False
        logger.debug("Reading file '{0}'".format(fp))
        fp = open(fp, 'rb')
    elif fp is None or not hasattr(fp, "read") or not hasattr(fp, "seek"):
        raise TypeError("dcmread: Expected a file path or a file-like, "
                        "but got " + type(fp).__name__)

    if config.debugging:
        logger.debug("\n" + "-" * 80)
        logger.debug("Call to dcmread()")
        msg = ("filename:'%s', defer_size='%s', "
               "stop_before_pixels=%s, force=%s, specific_tags=%s")
        logger.debug(msg % (fp.name, defer_size, stop_before_pixels,
                            force, specific_tags))
        if caller_owns_file:
            logger.debug("Caller passed file object")
        else:
            logger.debug("Caller passed file name")
        logger.debug("-" * 80)

    if specific_tags:
        specific_tags = [Tag(t) for t in specific_tags]

    specific_tags = cast(Optional[List[BaseTag]], specific_tags)

    # Iterate through all items and store them --include file meta if present
    stop_when = None
    if stop_before_pixels:
        stop_when = _at_pixel_data
    try:
        dataset = read_partial(
            fp,
            stop_when,
            defer_size=size_in_bytes(defer_size),
            force=force,
            specific_tags=specific_tags,
        )
    finally:
        if not caller_owns_file:
            fp.close()
    # XXX need to store transfer syntax etc.
    return dataset


def __getattr__(name: str) -> Any:
    if name == 'read_file':
        warnings.warn(
            "'read_file' is deprecated and will be removed in v3.0, use "
            "'dcmread' instead",
            DeprecationWarning
        )
        return globals()['dcmread']

    raise AttributeError(f"module {__name__} has no attribute {name}")


if sys.version_info[:2] < (3, 7):
    read_file = dcmread


def read_dicomdir(filename: PathType = "DICOMDIR") -> DicomDir:
    """Read a DICOMDIR file and return a :class:`~pydicom.dicomdir.DicomDir`.

    This is a wrapper around :func:`dcmread` which gives a default file name.

    .. deprecated:: 2.1

        ``read_dicomdir()`` is deprecated and will be removed in v3.0. Use
        :func:`~pydicom.filereader.dcmread` instead.

    Parameters
    ----------
    filename : str, optional
        Full path and name to DICOMDIR file to open

    Returns
    -------
    DicomDir

    Raises
    ------
    InvalidDicomError
        Raised if filename is not a DICOMDIR file.
    """
    warnings.warn(
        "'read_dicomdir()' is deprecated and will be removed in v3.0, use "
        "'dcmread()' instead",
        DeprecationWarning
    )

    str_or_obj = path_from_pathlike(filename)
    ds = dcmread(str_or_obj)
    if not isinstance(ds, DicomDir):
        raise InvalidDicomError(
            f"File '{filename!r}' is not a Media Storage Directory file"
        )

    return ds


def data_element_offset_to_value(
    is_implicit_VR: bool, VR: Optional[str]
) -> int:
    """Return number of bytes from start of data element to start of value"""
    if is_implicit_VR:
        return 8  # tag of 4 plus 4-byte length

    VR = cast(str, VR)
    if VR in extra_length_VRs:
        return 12  # tag 4 + 2 VR + 2 reserved + 4 length

    return 8  # tag 4 + 2 VR + 2 length


def read_deferred_data_element(
    fileobj_type: Any,
    filename_or_obj: Union[PathType, BinaryIO],
    timestamp: Optional[float],
    raw_data_elem: RawDataElement
) -> RawDataElement:
    """Read the previously deferred value from the file into memory
    and return a raw data element.

    .. note:

        This is called internally by pydicom and will normally not be
        needed in user code.

    Parameters
    ----------
    fileobj_type : type
        The type of the original file object.
    filename_or_obj : str or file-like
        The filename of the original file if one exists, or the file-like
        object where the data element persists.
    timestamp : float or None
        The time (as given by stat.st_mtime) the original file has been
        read, if not a file-like.
    raw_data_elem : dataelem.RawDataElement
        The raw data element with no value set.

    Returns
    -------
    dataelem.RawDataElement
        The data element with the value set.

    Raises
    ------
    IOError
        If `filename_or_obj` is ``None``.
    IOError
        If `filename_or_obj` is a filename and the corresponding file does
        not exist.
    ValueError
        If the VR or tag of `raw_data_elem` does not match the read value.
    """
    logger.debug("Reading deferred element %r" % str(raw_data_elem.tag))
    # If it wasn't read from a file, then return an error
    if filename_or_obj is None:
        raise IOError(
            "Deferred read -- original filename not stored. Cannot re-open"
        )

    # Check that the file is the same as when originally read
    is_filename = isinstance(filename_or_obj, str)
    if isinstance(filename_or_obj, str):
        if not os.path.exists(filename_or_obj):
            raise IOError(
                f"Deferred read -- original file {filename_or_obj} is missing"
            )

        if timestamp is not None:
            statinfo = os.stat(filename_or_obj)
            if statinfo.st_mtime != timestamp:
                warnings.warn(
                    "Deferred read warning -- file modification time has "
                    "changed"
                )

    # Open the file, position to the right place
    fp = (
        fileobj_type(filename_or_obj, 'rb') if is_filename
        else filename_or_obj
    )
    is_implicit_VR = raw_data_elem.is_implicit_VR
    is_little_endian = raw_data_elem.is_little_endian
    offset = data_element_offset_to_value(is_implicit_VR, raw_data_elem.VR)
    # Seek back to the start of the deferred element
    fp.seek(raw_data_elem.value_tell - offset)
    elem_gen = data_element_generator(
        fp, is_implicit_VR, is_little_endian, defer_size=None
    )

    # Read the data element and check matches what was stored before
    # The first element out of the iterator should be the same type as the
    #   the deferred element == RawDataElement
    elem = cast(RawDataElement, next(elem_gen))
    fp.close()
    if elem.VR != raw_data_elem.VR:
        raise ValueError(
            f"Deferred read VR {elem.VR} does not match original "
            f"{raw_data_elem.VR}"
        )

    if elem.tag != raw_data_elem.tag:
        raise ValueError(
            f"Deferred read tag {elem.tag!r} does not match "
            f"original {raw_data_elem.tag!r}"
        )

    # Everything is ok, now this object should act like usual DataElement
    return elem


def _get_bot(fp: DicomFile, number_of_frames: int) -> List[int]:
    """Tries to read the value of the Basic Offset Table (BOT) item and builds
    it in case it is empty.

    Parameters
    ----------
    fp: pydicom.filebase.DicomFile
        Pointer for DICOM PS3.10 file stream positioned at the first byte of
        the Pixel Data element
    number_of_frames: int
        Number of frames contained in the Pixel Data element

    Returns
    -------
    List[int]
        Offset of each Frame item in bytes from the first byte of the Pixel
        Data element following the BOT item

    Note
    ----
    Moves the pointer to the first byte of the open file following the BOT item
    (the first byte of the first Frame item).

    """
    logger.debug('read Basic Offset Table')
    basic_offset_table = _read_bot(fp)

    first_frame_offset = fp.tell()
    tag = TupleTag(fp.read_tag())
    if int(tag) != ItemTag:
        raise ValueError('Reading of Basic Offset Table failed')
    fp.seek(first_frame_offset, 0)

    # Basic Offset Table item must be present, but it may be empty
    if len(basic_offset_table) == 0:
        logger.debug('Basic Offset Table item is empty')
    if len(basic_offset_table) != number_of_frames:
        logger.debug('build Basic Offset Table item')
        basic_offset_table = _build_bot(
            fp,
            number_of_frames=number_of_frames
        )

    return basic_offset_table


def _read_bot(fp: DicomFile) -> List[int]:
    """Reads the Basic Offset Table (BOT) item of an encapsulated Pixel Data
    element.

    Parameters
    ----------
    fp: pydicom.filebase.DicomFile
        Pointer for DICOM PS3.10 file stream positioned at the first byte of
        the Pixel Data element

    Returns
    -------
    List[int]
        Offset of each Frame item in bytes from the first byte of the Pixel
        Data element following the BOT item

    Note
    ----
    Moves the pointer to the first byte of the open file following the BOT item
    (the first byte of the first Frame item).

    Raises
    ------
    IOError
        When file pointer is not positioned at first byte of Pixel Data element

    """
    tag = TupleTag(fp.read_tag())
    if int(tag) not in _PIXEL_DATA_TAGS:
        raise IOError(
            'Expected file pointer at first byte of Pixel Data element.'
        )
    # Skip Pixel Data element header (tag, VR, length)
    pixel_data_element_value_offset = data_element_offset_to_value(
        fp.is_implicit_VR, 'OB'
    )
    fp.seek(pixel_data_element_value_offset - 4, 1)
    is_empty, offsets = get_frame_offsets(fp)
    return offsets


def _build_bot(fp: DicomFile, number_of_frames: int) -> List[int]:
    """Builds a Basic Offset Table (BOT) item of an encapsulated Pixel Data
    element.

    Parameters
    ----------
    fp: pydicom.filebase.DicomFile
        Pointer for DICOM PS3.10 file stream positioned at the first byte of
        the Pixel Data element following the empty Basic Offset Table (BOT)
    number_of_frames: int
        Total number of frames in the dataset

    Returns
    -------
    List[int]
        Offset of each Frame item in bytes from the first byte of the Pixel
        Data element following the BOT item

    Note
    ----
    Moves the pointer back to the first byte of the Pixel Data element
    following the BOT item (the first byte of the first Frame item).

    Raises
    ------
    IOError
        When file pointer is not positioned at first byte of first Frame item
        after Basic Offset Table item or when parsing of Frame item headers
        fails
    ValueError
        When the number of offsets doesn't match the specified number of frames

    """
    initial_position = fp.tell()
    offset_values = []
    current_offset = 0
    i = 0
    while True:
        frame_position = fp.tell()
        tag = TupleTag(fp.read_tag())
        if int(tag) == SequenceDelimiterTag:
            break
        if int(tag) != ItemTag:
            fp.seek(initial_position, 0)
            raise IOError(
                'Building Basic Offset Table (BOT) failed. Expected tag of '
                f'Frame item #{i} at position {frame_position}.'
            )
        length = fp.read_UL()
        if length % 2:
            fp.seek(initial_position, 0)
            raise IOError(
                'Building Basic Offset Table (BOT) failed. '
                f'Length of Frame item #{i} is not a multiple of 2.'
            )
        elif length == 0:
            fp.seek(initial_position, 0)
            raise IOError(
                'Building Basic Offset Table (BOT) failed. '
                f'Length of Frame item #{i} is zero.'
            )

        first_two_bytes = fp.read(2, 1)
        if not fp.is_little_endian:
            first_two_bytes = first_two_bytes[::-1]

        # In case of fragmentation, we only want to get the offsets to the
        # first fragment of a given frame. We can identify those based on the
        # JPEG and JPEG 2000 markers that should be found at the beginning and
        # end of the compressed byte stream.
        if first_two_bytes in _START_MARKERS:
            current_offset = frame_position - initial_position
            offset_values.append(current_offset)

        i += 1
        fp.seek(length - 2, 1)  # minus the first two bytes

    if len(offset_values) != number_of_frames:
        raise ValueError(
            'Number of frame items does not match specified Number of Frames.'
        )
    else:
        basic_offset_table = offset_values

    fp.seek(initial_position, 0)
    return basic_offset_table


class ImageFileReader:

    """Reader for DICOM datasets representing Image Information Entities.

    It provides efficient access to individual Frame items contained in the
    Pixel Data element without loading the entire element into memory.

    Attributes
    ----------
    filename: str
        Path to the DICOM Part10 file on disk

    Examples
    --------
    >>> from pydicom.filereader import ImageFileReader
    >>> with ImageFileReader('/path/to/file.dcm') as image:
    ...     print(image.dataset)
    ...     for i in range(image.number_of_frames):
    ...         frame = image.read_frame(i)
    ...         print(frame.shape)

    """

    def __init__(self, fp: Union[PathType, BinaryIO, DicomFileLike]):
        """
        Parameters
        ----------
        fp: Union[str, pathlib.Path, pydicom.filebase.DicomfileLike]
            DICOM Part10 file containing a dataset of an image SOP Instance

        """
        if isinstance(fp, DicomFileLike):
            self._fp = fp
            self._filename = None
        elif isinstance(fp, (str, Path)):
            self._filename = Path(fp)
            self._fp = None
        else:
            is_little_endian, is_implicit_VR = self._check_file_format(fp)
            self._fp = DicomFileLike(fp, is_little_endian=is_little_endian, is_implicit_VR=is_implicit_VR)
            self._fp.is_little_endian = is_little_endian
            self._fp.is_implicit_VR = is_implicit_VR
            self._filename = None
        self._dataset = None

    def __enter__(self) -> 'ImageFileReader':
        self.open()
        return self

    def __exit__(self, except_type, except_value, except_trace) -> None:
        self._fp.close()
        if except_value:
            sys.stdout.write(
                'Error while accessing file "{}":\n{}'.format(
                    self._filename, str(except_value)
                )
            )
            for tb in traceback.format_tb(except_trace):
                sys.stdout.write(tb)
            raise

    def open(self) -> None:
        """Open file for reading.

        Raises
        ------
        FileNotFoundError
            When file cannot be found
        OSError
            When file cannot be opened
        IOError
            When DICOM dataset cannot be read from file
        ValueError
            When DICOM dataset contained in file does not represent an image

        Note
        ----
        Builds a Basic Offset Table to speed up subsequent frame-level access.

        """
        logger.debug('read File Meta Information')
        if self._fp is None:
            try:
                self._fp = DicomFile(str(self._filename), mode='rb')
            except FileNotFoundError:
                raise FileNotFoundError(f'File not found: "{self._filename}"')
            except Exception:
                raise OSError(
                    f'Could not open file for reading: "{self._filename}"'
                )
        is_little_endian, is_implicit_VR = self._check_file_format(self._fp)
        self._fp.is_little_endian = is_little_endian
        self._fp.is_implicit_VR = is_implicit_VR

    def _check_file_format(self, fp) -> Tuple[bool, bool]:
        """Check whether file object represents a DICOM Part 10 file.

        Parameters
        ----------
        fp: pydicom.filebase.DicomFileLike
            DICOM file object

        Returns
        -------
        is_little_endian: bool
            Whether the data set is encoded in little endian transfer syntax
        is_implicit_VR: bool
            Whether value representations of data elements in the data set
            are implicit

        Raises
        ------
        InvalidDicomError
            If the file object does not represent a DICOM Part 10 file

        """
        pos = fp.tell()
        read_preamble(fp, False)
        file_meta = _read_file_meta_info(fp)
        fp.seek(pos)
        transfer_syntax_uid = pydicom.uid.UID(file_meta.TransferSyntaxUID)
        return (
            transfer_syntax_uid.is_little_endian,
            transfer_syntax_uid.is_implicit_VR,
        )

    def _read_dataset(self) -> None:
        """Read the dataset metadata from file.

        Caches the dataset and additional information such as the offset of
        the Pixel Data element and the Basic Offset Table to speed up
        subsequent access to individual frame items.

        """
        logger.debug('read metadata elements')
        if self._fp is None:
            raise IOError('File has not been opened for reading.')

        try:
            dataset = dcmread(self._fp, stop_before_pixels=True)
        except Exception as err:
            raise IOError(f'DICOM metadata cannot be read from file: "{err}"')

        # Cache Transfer Syntax UID, since we need it to decode frame items
        self._transfer_syntax_uid = pydicom.uid.UID(
            dataset.file_meta.TransferSyntaxUID
        )

        # Construct a new Dataset that is fully decoupled from the file, i.e.,
        # that does not contain any File Meta Information
        del dataset.file_meta
        self._dataset = Dataset(dataset)

        self._pixel_data_offset = self._fp.tell()
        # Determine whether dataset contains a Pixel Data element
        try:
            tag = TupleTag(self._fp.read_tag())
        except EOFError:
            raise ValueError(
                'Dataset does not represent an image information entity.'
            )
        if int(tag) not in _PIXEL_DATA_TAGS:
            raise ValueError(
                'Dataset does not represent an image information entity.'
            )
        self._as_float = False
        if int(tag) in _FLOAT_PIXEL_DATA_TAGS:
            self._as_float = True

        # Reset the file pointer to the beginning of the Pixel Data element
        self._fp.seek(self._pixel_data_offset, 0)

        logger.debug('build Basic Offset Table')
        number_of_frames = self.number_of_frames
        if self._transfer_syntax_uid.is_encapsulated:
            try:
                self._basic_offset_table = _get_bot(
                    self._fp,
                    number_of_frames=number_of_frames
                )
            except Exception as err:
                raise IOError(f'Failed to build Basic Offset Table: "{err}"')
            self._first_frame_offset = self._fp.tell()
        else:
            if self._fp.is_implicit_VR:
                header_offset = 4 + 4  # tag and length
            else:
                header_offset = 4 + 2 + 2 + 4  # tag, VR, reserved and length
            self._first_frame_offset = self._pixel_data_offset + header_offset
            n_pixels = self._pixels_per_frame
            bits_allocated = self._dataset.BitsAllocated
            if bits_allocated == 1:
                self._basic_offset_table = [
                    int(math.floor(i * n_pixels / 8))
                    for i in range(number_of_frames)
                ]
            else:
                self._basic_offset_table = [
                    i * self._bytes_per_frame_uncompressed
                    for i in range(number_of_frames)
                ]

        if len(self._basic_offset_table) != number_of_frames:
            raise ValueError(
                'Length of Basic Offset Table does not match Number of Frames.'
            )

    @property
    def dataset(self) -> Dataset:
        """pydicom.dataset.Dataset: Metadata"""
        if self._dataset is None:
            self._read_dataset()
        return self._dataset

    def _decode_frame(self, value: bytes):
        """Decode pixel data of an individual frame.

        Parameters
        ----------
        value: bytes
            Pixel data of a frame (potentially compressed in case
            of encapsulated format encoding, depending on the transfer syntax)

        Returns
        -------
        numpy.ndarray
            Decoded pixel data

        Raises
        ------
        ValueError
            When transfer syntax is not supported.

        """
        # This hack creates a small dataset containing a Pixel Data element
        # with only a single frame item, which can then be decoded using the
        # existing pydicom API.
        ds = Dataset()
        ds.file_meta = FileMetaDataset()
        ds.file_meta.TransferSyntaxUID = self._transfer_syntax_uid
        ds.Rows = self.dataset.Rows
        ds.Columns = self.dataset.Columns
        ds.SamplesPerPixel = self.dataset.SamplesPerPixel
        ds.PhotometricInterpretation = self.dataset.PhotometricInterpretation
        ds.PixelRepresentation = self.dataset.PixelRepresentation
        ds.PlanarConfiguration = self.dataset.get('PlanarConfiguration', None)
        ds.BitsAllocated = self.dataset.BitsAllocated
        ds.BitsStored = self.dataset.BitsStored
        ds.HighBit = self.dataset.HighBit

        if self._transfer_syntax_uid.is_encapsulated:
            ds.PixelData = encapsulate(frames=[value])
        else:
            ds.PixelData = value

        return ds.pixel_array

    @property
    def _pixels_per_frame(self) -> int:
        """int: Number of pixels per frame"""
        return int(math.prod([
            self.dataset.Rows,
            self.dataset.Columns,
            self.dataset.SamplesPerPixel
        ]))

    @property
    def _bytes_per_frame_uncompressed(self) -> int:
        """int: Number of bytes per frame when uncompressed"""
        n_pixels = self._pixels_per_frame
        bits_allocated = self.dataset.BitsAllocated
        if bits_allocated == 1:
            # Determine the nearest whole number of bytes needed to contain
            #   1-bit pixel data. e.g. 10 x 10 1-bit pixels is 100 bits, which
            #   are packed into 12.5 -> 13 bytes
            return n_pixels // 8 + (n_pixels % 8 > 0)
        else:
            return n_pixels * bits_allocated // 8

    def close(self) -> None:
        """Close file."""
        self._fp.close()

    def read_frame_raw(self, index: int) -> bytes:
        """Read the raw pixel data of an individual frame item.

        Parameters
        ----------
        index: int
            Zero-based frame index

        Returns
        -------
        bytes
            Pixel data of a given frame item encoded in the transfer syntax.

        Raises
        ------
        IOError
            When frame could not be read

        """
        if index > self.number_of_frames:
            raise ValueError('Frame index exceeds number of frames in image.')

        logger.debug(f'read frame #{index}')

        if self._dataset is None:
            self._read_dataset()

        frame_offset = self._basic_offset_table[index]
        self._fp.seek(self._first_frame_offset + frame_offset, 0)
        if self._transfer_syntax_uid.is_encapsulated:
            try:
                stop_at = self._basic_offset_table[index + 1] - frame_offset
            except IndexError:
                # For the last frame, there is no next offset available.
                stop_at = -1
            n = 0
            # A frame may consist of multiple items (fragments).
            fragments = []
            while True:
                tag = TupleTag(self._fp.read_tag())
                if n == stop_at or int(tag) == SequenceDelimiterTag:
                    break
                if int(tag) != ItemTag:
                    raise ValueError(f'Failed to read frame #{index}.')
                length = self._fp.read_UL()
                fragments.append(self._fp.read(length))
                n += 4 + 4 + length
            frame_data = b''.join(fragments)
        else:
            frame_data = self._fp.read(self._bytes_per_frame_uncompressed)

        if len(frame_data) == 0:
            raise IOError(f'Failed to read frame #{index}.')

        return frame_data

    def read_frame(self, index: int):
        """Read and decode the pixel data of an individual frame item.

        Parameters
        ----------
        index: int
            Zero-based frame index

        Returns
        -------
        numpy.ndarray
            Array of decoded pixels of the frame with shape (Rows x Columns)
            in case of a monochrome image or (Rows x Columns x SamplesPerPixel)
            in case of a color image.

        Raises
        ------
        IOError
            When frame could not be read

        """
        frame_data = self.read_frame_raw(index)

        logger.debug(f'decode frame #{index}')

        if self.dataset.BitsAllocated == 1:
            unpacked_frame = unpack_bits(frame_data)
            rows, columns = self.dataset.Rows, self.dataset.Columns
            n_pixels = self._pixels_per_frame
            pixel_offset = int(((index * n_pixels / 8) % 1) * 8)
            pixel_array = unpacked_frame[pixel_offset:pixel_offset + n_pixels]
            return pixel_array.reshape(rows, columns)

        frame_array = self._decode_frame(frame_data)

        return frame_array

    @property
    def number_of_frames(self) -> int:
        """int: Number of frames"""
        try:
            return int(self.dataset.NumberOfFrames)
        except AttributeError:
            return 1
