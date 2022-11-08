# Copyright 2008-2022 pydicom authors. See LICENSE file for details.
"""Utilities for parsing DICOM PixelData frames.

Adapted from @hackermd's ImageFileReader PR (#1447)
"""
import math
import sys
import traceback
import warnings
from io import StringIO, BytesIO
from pathlib import Path
from typing import List, Tuple, Union, BinaryIO, Optional, Any

try:
    import numpy
except ImportError:
    pass

from pydicom import Dataset
from pydicom.config import logger
from pydicom.dataset import FileMetaDataset, _DatasetType
from pydicom.encaps import encapsulate, get_frame_offsets
from pydicom.errors import InvalidDicomError
from pydicom.filebase import DicomFile, DicomFileLike
from pydicom.filereader import (
    read_partial,
    _is_implicit_vr,
    data_element_offset_to_value,
)
from pydicom.fileutil import PathType
from pydicom.pixel_data_handlers import unpack_bits
from pydicom.tag import TupleTag, ItemTag, SequenceDelimiterTag, BaseTag
from pydicom.uid import UID

_FLOAT_PIXEL_DATA_TAGS = {
    0x7FE00008,
    0x7FE00009,
}
_UINT_PIXEL_DATA_TAGS = {
    0x7FE00010,
}
_PIXEL_DATA_TAGS = _FLOAT_PIXEL_DATA_TAGS.union(_UINT_PIXEL_DATA_TAGS)

_JPEG_SOI_MARKER = b"\xFF\xD8"  # also JPEG-LS
_JPEG_EOI_MARKER = b"\xFF\xD9"  # also JPEG-LS
_JPEG2000_SOC_MARKER = b"\xFF\x4F"
_JPEG2000_EOC_MARKER = b"\xFF\xD9"
_START_MARKERS = {_JPEG_SOI_MARKER, _JPEG2000_SOC_MARKER}
_END_MARKERS = {_JPEG_EOI_MARKER, _JPEG2000_EOC_MARKER}
_REQUIRED_DATASET_ATTRIBUTES = {
    "BitsAllocated",
    "BitsStored",
    "Columns",
    "HighBit",
    "PhotometricInterpretation",
    "PixelRepresentation",
    "Rows",
    "SamplesPerPixel",
}


def read_encapsulated_basic_offset_table(
    fp: DicomFileLike, pixel_data_location: int
) -> Tuple[int, List[int]]:
    """Reads the Basic Offset Table (BOT) item of an encapsulated Pixel Data
    element.

    Parameters
    ----------
    fp: pydicom.filebase.DicomFileLike
        Pointer for DICOM PS3.10 file stream positioned at the first byte of
        the Pixel Data element
    pixel_data_location: int
        PixelData data element location (file_obj.tell() at stop_before_pixels)

    Returns
    -------
    Tuple[int, List[int]]
        A tuple of the first frame location and a list of relative offsets where
        the offset of each Frame item in bytes from the first byte of the Pixel
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
    if fp.tell() != pixel_data_location:
        fp.seek(pixel_data_location, 0)

    # calculate and seek to first frame location
    ob_offset = data_element_offset_to_value(fp.is_implicit_VR, "OB")
    basic_offset_table_location = pixel_data_location + ob_offset
    fp.seek(basic_offset_table_location, 0)

    has_bot, frame_offsets = get_frame_offsets(fp)
    first_frame_location = fp.tell()
    return first_frame_location, frame_offsets


def get_encapsulated_basic_offset_table(
    fp: DicomFileLike, pixel_data_location: int, number_of_frames: int
) -> List[int]:
    """Tries to read the value of the Basic Offset Table (BOT) item and builds
    it in case it is empty.

    Parameters
    ----------
    fp: pydicom.filebase.DicomFileLike
        Pointer for DICOM PS3.10 file stream positioned at the first byte of
        the Pixel Data element

    number_of_frames: int
        Number of frames contained in the Pixel Data element
    pixel_data_location: int
        location of PixelData data element within file_obj
    number_of_frames: int
        the expected number of frames
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
    logger.debug("Reading Basic Offset Table at position %i", pixel_data_location)
    first_frame_location, basic_offset_table = read_encapsulated_basic_offset_table(
        fp, pixel_data_location
    )
    tag = TupleTag(fp.read_tag())
    if int(tag) != ItemTag:
        raise ValueError(
            f"Reading of Basic Offset Table failed - expected tag {ItemTag} "
            f"at location {pixel_data_location} but found tag {tag}"
        )

    if len(basic_offset_table) < number_of_frames:
        logger.debug("build Basic Offset Table item")
        basic_offset_table = build_encapsulated_basic_offset_table(
            fp,
            first_frame_location=first_frame_location,
            number_of_frames=number_of_frames,
        )
    else:
        fp.seek(first_frame_location, 0)

    return basic_offset_table


def build_encapsulated_basic_offset_table(
    fp: DicomFileLike, first_frame_location: int, number_of_frames: int
) -> List[int]:
    """Builds a Basic Offset Table (BOT) item of an encapsulated Pixel Data
    element.

    Parameters
    ----------
    fp: pydicom.filebase.DicomFileLike
        Pointer for DICOM PS3.10 file stream positioned at the first byte of
        the Pixel Data element following the empty Basic Offset Table (BOT)
    first_frame_location: location of the first frame item
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
    if fp.tell() != first_frame_location:
        fp.seek(first_frame_location, 0)
    offset_values = []
    fragment_offsets = []
    i = 0
    while True:
        frame_position = fp.tell()
        tag = TupleTag(fp.read_tag())
        if int(tag) == SequenceDelimiterTag:
            break
        if int(tag) != ItemTag:
            fp.seek(first_frame_location, 0)
            raise IOError(
                "Building Basic Offset Table (BOT) failed. Expected tag of "
                f"Frame item #{i} at position {frame_position}, but found "
                f"tag {tag}"
            )
        length = fp.read_UL()
        if length % 2:
            fp.seek(first_frame_location, 0)
            raise IOError(
                "Building Basic Offset Table (BOT) failed. "
                f"Length of Frame item #{i} is not a multiple of 2."
            )
        elif length == 0:
            fp.seek(first_frame_location, 0)
            raise IOError(
                "Building Basic Offset Table (BOT) failed. "
                f"Length of Frame item #{i} is zero."
            )
        fragment_offsets.append(frame_position - first_frame_location)
        first_two_bytes = fp.read(2, True)
        if not fp.is_little_endian:
            first_two_bytes = first_two_bytes[::-1]

        # In case of fragmentation, we only want to get the offsets to the
        # first fragment of a given frame. We can identify those based on the
        # JPEG and JPEG 2000 markers that should be found at the beginning and
        # end of the compressed byte stream.
        if first_two_bytes in _START_MARKERS:
            current_offset = frame_position - first_frame_location
            offset_values.append(current_offset)

        i += 1
        fp.seek(length - 2, 1)  # minus the first two bytes
    # RLE and others with 1:1 fragment:frame
    if len(fragment_offsets) == number_of_frames and not offset_values:
        offset_values = fragment_offsets
    if len(offset_values) != number_of_frames:
        raise ValueError(
            f"Number of frame items {len(offset_values)} does not match "
            f"specified Number of Frames {number_of_frames}."
        )
    else:
        basic_offset_table = offset_values

    fp.seek(first_frame_location, 0)
    return basic_offset_table


def get_dataset_copy_with_frame_attrs(original_dataset: Dataset) -> Dataset:
    """Create a copy of original_dataset with only the data elements needed
    to parse frames

    Parameters
    ----------
    original_dataset: Dataset
        the dataset for which to create a minimal copy
    Returns
    -------
    Dataset
        the copy of original_dataset
    """
    ds_copy = Dataset()
    ds_copy.file_meta = FileMetaDataset()
    ds_copy.file_meta.TransferSyntaxUID = original_dataset.file_meta.TransferSyntaxUID
    ds_copy.PlanarConfiguration = original_dataset.get("PlanarConfiguration", None)
    required_attributes = (
        "Rows",
        "Columns",
        "SamplesPerPixel",
        "PhotometricInterpretation",
        "PixelRepresentation",
        "BitsAllocated",
        "BitsStored",
        "HighBit",
    )
    for r_attribute in required_attributes:
        original_value = getattr(original_dataset, r_attribute)
        setattr(ds_copy, r_attribute, original_value)

    return ds_copy


def decode_frame(frame_bytes: bytes, original_dataset: Dataset) -> "numpy.ndarray":
    """Decode the frame_bytes provided from original_dataset as a numpy ndarray

    Parameters
    ----------
    frame_bytes: bytes
        the bytes corresponding to a frame within DICOM PixelData
    original_dataset: Dataset
        the original_dataset representing the file from which frame_bytes were
        retrieved

    Returns
    -------
    numpy.ndarray
        array representation of frame_bytes
    """
    ds_temp = get_dataset_copy_with_frame_attrs(original_dataset)
    if ds_temp.file_meta.TransferSyntaxUID.is_encapsulated:
        ds_temp.PixelData = encapsulate(frames=[frame_bytes])
    else:
        ds_temp.PixelData = frame_bytes

    return ds_temp.pixel_array


class BasicOffsetTable(List):
    """A class for storing Basic Offset Table information needed to read
    multi-frame DICOMs

    Parameters
    ----------
    pixel_data_location: int
        the location within a file where the PixelData tag is located
    first_frame_location: int
        the location of the first byte of the first frame of PixelData

    Examples
    --------
    Create a basic offset table for liver.dcm
    >>> bot = BasicOffsetTable([0, 32768, 65536], pixel_data_location=4314, first_frame_location=4326)
    """

    def __init__(self, *args: List[int], **kwargs: int) -> None:
        self.first_frame_location: int = kwargs.pop("first_frame_location")
        self.pixel_data_location: int = kwargs.pop("pixel_data_location")
        super().__init__(*args, **kwargs)

    def to_dict(self) -> dict:
        return {
            "basic_offset_table": self,
            "first_frame_location": self.first_frame_location,
            "pixel_data_location": self.pixel_data_location,
        }

    @classmethod
    def from_dict(cls, info_dict: dict) -> "BasicOffsetTable":
        return cls(
            info_dict["basic_offset_table"],
            first_frame_location=info_dict["first_frame_location"],
            pixel_data_location=info_dict["pixel_data_location"],
        )


class FrameDataset(Dataset):
    """A subclass of Dataset for validation and parsing of DICOMs which contain
    PixelData

    Attributes
    ----------
    pixels_per_frame: int
        the number of pixels found in each frame of PixelData
    """

    def __init__(self, *args: Dataset, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # These don't get copied over, need to manually set
        if hasattr(args[0], "file_meta"):
            setattr(self, "file_meta", getattr(args[0], "file_meta"))
        self.is_implicit_VR: bool = args[0].is_implicit_VR  # type: ignore[assignment]
        self.is_little_endian: bool = args[0].is_little_endian  # type: ignore[assignment]

        self.validate_frame_dataset()
        self.pixels_per_frame = int(self.Rows * self.Columns * self.SamplesPerPixel)
        self._bytes_per_frame: Union[int, None] = None

    @property
    def bytes_per_frame(self) -> int:
        """int: Number of bytes per frame when uncompressed"""
        if not isinstance(self._bytes_per_frame, int):
            if self.BitsAllocated == 1:
                # Determine the nearest whole number of bytes needed to contain
                #   1-bit pixel data. e.g. 10 x 10 1-bit pixels is 100 bits, which
                #   are packed into 12.5 -> 13 bytes
                bytes_per_frame: int = self.pixels_per_frame // 8 + (self.pixels_per_frame % 8 > 0)
            else:
                bytes_per_frame = self.pixels_per_frame * self.BitsAllocated // 8
            self._bytes_per_frame = bytes_per_frame
        else:
            bytes_per_frame = self._bytes_per_frame
        return bytes_per_frame

    def _get_uncompressed_basic_offset_table(
        self, pixel_data_location: int
    ) -> BasicOffsetTable:
        """Get the locations of non-encapsulated PixelData frames relative to
        the first frame (which is always 0)

        Parameters
        ----------
        pixel_data_location: int
            the location of the PixelData DICOM tag within the file represented
            by FrameDataset

        Returns
        -------
        BasicOffsetTable: list of ints with pixel_data_location and
            first_frame_location attributes
        """
        if self.is_implicit_VR:
            header_offset = 4 + 4  # tag and length
        else:
            header_offset = 4 + 2 + 2 + 4  # tag, VR, reserved and length
        first_frame_location = pixel_data_location + header_offset
        if self.BitsAllocated == 1:
            basic_offset_table = [
                int(math.floor(i * self.pixels_per_frame / 8))
                for i in range(self.NumberOfFrames)
            ]
        else:
            basic_offset_table = [
                i * self.bytes_per_frame for i in range(self.NumberOfFrames)
            ]
        return BasicOffsetTable(
            basic_offset_table,
            pixel_data_location=pixel_data_location,
            first_frame_location=first_frame_location,
        )

    def _get_encapsulated_basic_offset_table(
        self, fp: DicomFileLike, pixel_data_location: int
    ) -> BasicOffsetTable:
        """Get the locations of encapsulated PixelData frames relative to
        the first frame (which is always 0)

        Parameters
        ----------
        pixel_data_location: int
            the location of the PixelData DICOM tag within the file represented
            by FrameDataset

        Returns
        -------
        BasicOffsetTable: list of ints with pixel_data_location and
            first_frame_location attributes
        """
        try:
            basic_offset_table = get_encapsulated_basic_offset_table(
                fp, pixel_data_location, number_of_frames=self.NumberOfFrames
            )
            first_frame_location = fp.tell()
            return BasicOffsetTable(
                basic_offset_table,
                pixel_data_location=pixel_data_location,
                first_frame_location=first_frame_location,
            )
        except Exception as err:
            raise IOError(f'Failed to build Basic Offset Table: "{err}"')

    def get_basic_offset_table(
        self, fp: DicomFileLike, pixel_data_location: int
    ) -> BasicOffsetTable:
        """Get the locations of PixelData frames relative to the first frame
        (which is always 0)

        Parameters
        ----------
        fp: DicomFileLike
            the DicomFileLike for which to calculate the basic offset table
        pixel_data_location: int
            the location of the PixelData DICOM tag within the file represented
            by FrameDataset

        Returns
        -------
        BasicOffsetTable: list of ints with pixel_data_location and
            first_frame_location attributes

        Raises
        ------
        ValueError
            If the length of the computed basic offset table does not match the
            FrameDataset's NumberOfFrames
        """
        if self.file_meta.TransferSyntaxUID.is_encapsulated:
            bot = self._get_encapsulated_basic_offset_table(fp, pixel_data_location)
        else:
            bot = self._get_uncompressed_basic_offset_table(pixel_data_location)
        return bot

    def validate_frame_dataset(self) -> None:
        """Ensure that the necessary attributes for parsing frame data are
        present. fix_meta_info is performed in the case that TransferSyntaxUID
        is missing. NumberOfFrames is inferred as 1 if absent.
        """
        if not hasattr(self.file_meta, "TransferSyntaxUID"):
            warnings.warn("Missing TransferSyntaxUID, attempting to infer...")
            self.fix_meta_info(enforce_standard=False)
            if not hasattr(self.file_meta, "TransferSyntaxUID"):
                raise IOError(
                    "Failed to infer TransferSyntaxUID. Cannot read frames "
                    "for dataset."
                )
        number_of_frames = getattr(self, "NumberOfFrames", None)
        if not isinstance(number_of_frames, int):
            warnings.warn("Dataset missing valid NumberOfFrames, inferring as 1")
            setattr(self, "NumberOfFrames", 1)
        missing = list()
        for keyword in _REQUIRED_DATASET_ATTRIBUTES:
            if not hasattr(self, keyword):
                missing.append(keyword)
        if missing:
            raise IOError(
                f"DICOM is missing required attributes: {missing}. Cannot "
                "read frames for dataset"
            )

    @staticmethod
    def read_dataset(
        file_obj: BinaryIO,
        defer_size: Optional[Union[int, str, float]] = None,
        force: bool = False,
        specific_tags: Optional[List[BaseTag]] = None,
    ) -> Dataset:
        """Read a Dataset from fp, stopping at PixelData

        Parameters
        ----------
        file_obj: Union[DicomFileLike, BinaryIO]
            a file-like object
        defer_size: int, str or float, optional
            See :func:`dcmread` for parameter info.
        force: bool
            See :func:`dcmread` for parameter info.
        specific_tags: list or None
            See :func:`dcmread` for parameter info.

        Returns
        -------
        Dataset
            dataset representing file_obj

        Raises
        ------
        IOError
            When the file is missing PixelData or the Dataset otherwise cannot
            be parsed.
        """
        stopped_at_pixel_data = False

        def _pixel_data_stop_when(tag: BaseTag, vr: Optional[str], length: int) -> bool:
            nonlocal stopped_at_pixel_data
            stopped_at_pixel_data = tag in {0x7FE00010, 0x7FE00009, 0x7FE00008}
            return stopped_at_pixel_data

        try:

            dataset = read_partial(
                file_obj,
                stop_when=_pixel_data_stop_when,
                defer_size=defer_size,
                force=force,
                specific_tags=specific_tags,
            )
            file_pos = file_obj.tell()
            true_stop = lambda x, y, z: True
            implicit_vr = _is_implicit_vr(
                file_obj,
                dataset.is_implicit_VR,
                dataset.is_little_endian,
                stop_when=true_stop,
                is_sequence=False,
            )
            file_obj.seek(file_pos)
            if implicit_vr != dataset.is_implicit_VR:
                setattr(dataset, "is_implicit_VR", implicit_vr)
        except InvalidDicomError as exc:
            logger.error(
                "Exception raised when attempting to parse dataset", exc_info=True
            )
            raise IOError(
                f"Failed to read dataset for file due to exception ({exc}). "
                "Cannot read frames for dataset."
            )
        except Exception as exc:
            logger.error(
                "Exception raised when attempting to parse dataset", exc_info=True
            )
            raise IOError(
                f"Failed to read dataset for file due to exception ({exc}). "
                "Cannot read frames for dataset."
            )
        if not stopped_at_pixel_data:
            raise IOError("Cannot read frames for dataset - missing PixelData")
        return dataset

    def to_info_dict(self) -> dict:
        ds_copy = get_dataset_copy_with_frame_attrs(self)
        return {
            "dicom_json": ds_copy.to_json_dict(),
            "is_little_endian": ds_copy.is_little_endian,
            "is_implicit_VR": ds_copy.is_implicit_VR,
            "TransferSyntaxUID": self.file_meta.TransferSyntaxUID,
        }

    @classmethod
    def from_file(
        cls,
        fp: BinaryIO,
        defer_size: Optional[Union[int, str, float]] = None,
        force: bool = False,
        specific_tags: Optional[List[BaseTag]] = None,
    ) -> "FrameDataset":
        """instantiate `FrameDataset` from a file-like object

        Parameters
        ----------
        fp: DicomFileLike or BinaryIO
            the file-like object for which to instantiate FrameInfo
        defer_size: int, str or float, optional
            See :func:`dcmread` for parameter info.
        force: bool
            See :func:`dcmread` for parameter info.
        specific_tags: list or None
            See :func:`dcmread` for parameter info.

        Returns
        -------
        FrameDataset

        """
        dataset = cls.read_dataset(
            file_obj=fp,
            defer_size=defer_size,
            force=force,
            specific_tags=specific_tags,
        )
        return cls(dataset)

    @classmethod
    def from_info_dict(cls, info_dict: dict) -> "FrameDataset":
        new_dataset = Dataset.from_json(info_dict["dicom_json"])
        for attr in ("is_little_endian", "is_implicit_VR"):
            setattr(new_dataset, attr, info_dict[attr])
        if not hasattr(new_dataset, "file_meta"):
            new_dataset.fix_meta_info(enforce_standard=False)
        setattr(
            new_dataset.file_meta, "TransferSyntaxUID", info_dict["TransferSyntaxUID"]
        )
        frame_dataset = cls(new_dataset)
        return frame_dataset


class FrameInfo:
    """Class for storing attributes needed for parsing frames from DICOM
    PixelData

    Attributes
    ----------
    dataset: FrameDataset
        the dataset representing the DICOM instance
    basic_offset_table: BasicOffsetTable
        list representing the Basic Offset Table for the DICOM instance
    transfer_syntax_uid: UID
        TransferSyntaxUID for the DICOM instance
    """

    def __init__(
        self, dataset: FrameDataset, basic_offset_table: BasicOffsetTable,
    ) -> None:
        self.dataset = dataset
        self.transfer_syntax_uid = dataset.file_meta.TransferSyntaxUID
        self.basic_offset_table = basic_offset_table

    @staticmethod
    def validate_pixel_data(fp: DicomFileLike, pixel_data_location: int) -> None:
        """Validate that pixel data is present and parsable at
        pixel_data_location

        Parameters
        ----------
        fp: DicomFileLike
            the DicomFileLike for which to validate Pixel Data
        pixel_data_location: int
            the location of the PixelData DICOM tag within the file represented
            by FrameDataset
        """
        fp.seek(pixel_data_location, 0)
        # Determine whether dataset contains a Pixel Data element
        try:
            tag = TupleTag(fp.read_tag())
        except EOFError:
            raise IOError("Reached EOF while parsing PixelData")
        if int(tag) not in _PIXEL_DATA_TAGS:
            raise ValueError(
                f"PixelData not found at file location {pixel_data_location}."
                f"Tag found: {tag}"
            )

        # Reset the file pointer to the beginning of the Pixel Data element
        fp.seek(pixel_data_location, 0)

    def to_dict(self) -> dict:
        """create a dictionary representation of the FrameInfo instance for
        convenient storage/caching

        Returns
        -------
        dict
            dictionary with keys `basic_offset_table`, `dataset`, `transfer_syntax_uid`
        """
        return {
            "basic_offset_table": self.basic_offset_table.to_dict(),
            "dataset": self.dataset.to_info_dict(),
            "transfer_syntax_uid": self.transfer_syntax_uid,
        }

    @classmethod
    def from_file(
        cls,
        fp: BinaryIO,
        defer_size: Optional[Union[int, str, float]] = None,
        force: bool = False,
        specific_tags: Optional[List[BaseTag]] = None,
    ) -> "FrameInfo":
        """instantiate `FrameInfo` from a file-like object

        Parameters
        ----------
        fp: DicomFileLike or BinaryIO
            the file-like object for which to instantiate FrameInfo
        defer_size: int, str or float, optional
            See :func:`dcmread` for parameter info.
        force: bool
            See :func:`dcmread` for parameter info.
        specific_tags: list or None
            See :func:`dcmread` for parameter info.

        Returns
        -------
        FrameInfo

        """
        dataset = FrameDataset.from_file(
            fp=fp, defer_size=defer_size, force=force, specific_tags=specific_tags,
        )
        pixel_data_location = fp.tell()

        file_like = DicomFileLike(fp)

        file_like.is_little_endian = dataset.is_little_endian
        file_like.is_implicit_VR = dataset.is_implicit_VR
        cls.validate_pixel_data(file_like, pixel_data_location)
        basic_offset_table = dataset.get_basic_offset_table(file_like, pixel_data_location)
        return cls(dataset=dataset, basic_offset_table=basic_offset_table)

    @classmethod
    def from_dict(cls, frame_info_dict: dict) -> "FrameInfo":
        """instantiate `FrameInfo` from a dictionary generated by `to_dict`

        Parameters
        ----------
        frame_info_dict: dict
            dictionary output from `FrameInfo.to_dict`
        """
        dataset = FrameDataset.from_info_dict(frame_info_dict["dataset"])
        bot = BasicOffsetTable.from_dict(frame_info_dict["basic_offset_table"])
        return cls(dataset=dataset, basic_offset_table=bot)


class FrameReader:
    """Class for reading frames from DICOM files that contain PixelData

    Notably, it provides efficient access to individual frames contained in the
    Pixel Data element without loading the entire data element into memory.

    Attributes
    ----------
    _filename: str
        Path to the DICOM file (if path is provided rather than file-like
    frame_info: FrameInfo
        object containing the information required to parse frames
    defer_size: int, str or float, optional
        setting to provide to `filereader.read_partial` if not initialized with
        frame_info
    force: bool, optional
        setting to provide to `filereader.read_partial` if not initialized with
        frame_info
    specific_tags: list of (int or str or 2-tuple of int), optional
        setting to provide to `filereader.read_partial` if not initialized with
        frame_info
    number_of_frames: int
        number of frames in the DICOM

    Examples
    --------
    >>> from pydicom.framereader import FrameReader
    >>> with FrameReader('/path/to/file.dcm') as frame_reader:
    ...     print(frame_reader.dataset)
    ...     for i in range(frame_reader.dataset.NumberOfFrames):
    ...         frame = frame_reader.read_frame(i)
    ...         print(frame.shape)

    Notes
    -----
    This class is intended to be used exclusively as a context manager (i.e.
    only using `with` syntax as in the example above). Unpredictable and/or
    aberrant behavior may be encountered if invoked directly.
    """

    def __init__(
        self,
        file_like: Union[PathType, StringIO, BinaryIO, BytesIO],
        frame_info: Optional[FrameInfo] = None,
        defer_size: Optional[Union[int, str, float]] = None,
        force: bool = False,
        specific_tags: Optional[List[BaseTag]] = None,
    ):
        """
        Parameters
        ----------
        file_like: Union[PathType, StringIO, BinaryIO, BytesIO]
            the DICOM file from which to read frames
        frame_info: FrameInfo, optional
            if provided, parsing of the DICOM header can be circumvented,
            empowering developers to store/cache metadata which are comparatively
            much smaller than PixelData
        defer_size: int, str or float, optional
            setting to provide to `filereader.read_partial` if not initialized
            with frame_info
        force: bool, optional
            setting to provide to `filereader.read_partial` if not initialized
            with frame_info
        specific_tags: list of (int or str or 2-tuple of int), optional
            setting to provide to `filereader.read_partial` if not initialized
            with frame_info
        """
        if isinstance(file_like, (str, Path)):
            self._filename: Union[Path, None] = Path(file_like)
            self._fp = None
        else:
            self._filename = None
            self._fp = file_like
        self._dicom_file_like = None
        self._frame_info = frame_info
        self.defer_size = defer_size
        self.force = force
        self.specific_tags = specific_tags

        self._number_of_frames = None

    def __enter__(self) -> "FrameReader":
        self.open()
        return self

    def __exit__(self, except_type: Any, except_value: Any, except_trace: Any) -> None:
        self.fp.close()
        if except_value:
            sys.stdout.write(
                "Error while accessing file '{}':\n{}".format(
                    self._filename, str(except_value)
                )
            )
            for tb in traceback.format_tb(except_trace):
                sys.stdout.write(tb)
            raise

    def open(self) -> None:
        """Open file for reading and parse FrameInfo if unset.

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
        logger.debug("read File Meta Information")
        self.fp

    @property
    def fp(self) -> BinaryIO:
        if self._fp is None:
            try:
                # returns DicomFileLike
                self._fp = open(str(self._filename), mode="rb")
            except FileNotFoundError:
                raise FileNotFoundError(f"File not found: {self._filename}")
            except Exception:
                raise OSError(f"Could not open file for reading: {self._filename}")
        return self._fp   # type: ignore[return-value]

    @property
    def dicom_file_like(self) -> DicomFileLike:
        if self._dicom_file_like is None:
            dicom_file_like = DicomFileLike(self.fp)
            # set endian-ness and VR type on the DicomFileLike from FrameDataset
            dicom_file_like.is_little_endian = self.dataset.is_little_endian
            dicom_file_like.is_implicit_VR = self.dataset.is_implicit_VR
        else:
            dicom_file_like = self._dicom_file_like

        return dicom_file_like

    @property
    def frame_info(self) -> FrameInfo:
        if self._frame_info is None:
            self._frame_info = FrameInfo.from_file(
                self.fp,
                defer_size=self.defer_size,
                force=self.force,
                specific_tags=self.specific_tags,
            )
        return self._frame_info

    @property
    def dataset(self) -> FrameDataset:
        return self.frame_info.dataset

    @property
    def basic_offset_table(self) -> BasicOffsetTable:
        return self.frame_info.basic_offset_table

    @property
    def pixel_data_location(self) -> int:
        return self.basic_offset_table.pixel_data_location

    @property
    def first_frame_location(self) -> int:
        return self.basic_offset_table.first_frame_location

    @property
    def transfer_syntax_uid(self) -> UID:
        return self.dataset.file_meta.TransferSyntaxUID

    @property
    def number_of_frames(self) -> int:
        return len(self.basic_offset_table)

    def read_frame_raw(self, index: int) -> bytes:
        """Read the raw pixel data of an individual frame.

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
        ValueError
            When index > number of frames in file

        """
        if index + 1 > self.number_of_frames:
            raise ValueError(
                f"Frame index ({index}) exceeds number of frames in image "
                f"({self.number_of_frames})"
            )

        logger.debug("Reading frame data for frame %i", index)
        if self.transfer_syntax_uid.is_encapsulated:
            return self._read_compressed_frame_raw(index)
        else:
            return self._read_uncompressed_frame_raw(index)

    def _read_compressed_frame_raw(self, index: int) -> bytes:
        """Read the raw pixel data of an individual encapsulated frame.

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
        ValueError
            When index > number of frames in file

        """
        frame_offset = self.basic_offset_table[index]
        self.dicom_file_like.seek(self.first_frame_location + frame_offset, 0)
        try:
            stop_at = self.basic_offset_table[index + 1] - frame_offset
        except IndexError:
            # For the last frame, there is no next offset available.
            stop_at = -1
        n = 0
        # A frame may consist of multiple items (fragments).
        fragments = []
        while True:
            tag = TupleTag(self.dicom_file_like.read_tag())
            if n == stop_at or int(tag) == SequenceDelimiterTag:
                break
            if int(tag) != ItemTag:
                raise ValueError(
                    f"Failed to read data for frame #{index}. Found non-item "
                    f"tag {tag}, but expected {ItemTag}"
                )
            length = self.dicom_file_like.read_UL()
            fragments.append(self.fp.read(length))
            n += 4 + 4 + length
        frame_data = b"".join(fragments)
        return frame_data

    def _read_uncompressed_frame_raw(self, index: int) -> bytes:
        """Read the raw pixel data of an individual uncompressed frame.

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
        ValueError
            When index > number of frames in file

        """
        frame_offset = self.basic_offset_table[index]
        self.fp.seek(self.first_frame_location + frame_offset, 0)
        frame_data = self.fp.read(self.dataset.bytes_per_frame)
        return frame_data

    def read_frame(self, index: int) -> "numpy.ndarray":
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
        logger.debug(f"Decoding frame #{index}")
        if self.dataset.BitsAllocated == 1:
            unpacked_frame = unpack_bits(frame_data, True)
            rows, columns = self.dataset.Rows, self.dataset.Columns
            n_pixels = self.dataset.pixels_per_frame
            pixel_offset = int(((index * n_pixels / 8) % 1) * 8)
            pixel_array = unpacked_frame[pixel_offset : pixel_offset + n_pixels]
            if isinstance(pixel_array, numpy.ndarray):
                return pixel_array.reshape(rows, columns)

        frame_array = decode_frame(frame_data, self.dataset)
        return frame_array
