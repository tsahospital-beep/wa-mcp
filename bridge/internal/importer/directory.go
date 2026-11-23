package importer

import (
	"archive/zip"
	"bufio"
	"bytes"
	"encoding/binary"
	"fmt"
	"io"
)

// Bound the central directory before archive/zip allocates a File for each record.
// Chat exports are single-disk archives starting at offset zero.
func checkDirectory(src Source, lim Limits) error {
	const endSize = 22
	if src.Size < endSize {
		return zip.ErrFormat
	}
	tail := make([]byte, min(src.Size, endSize+65535))
	if _, err := src.R.ReadAt(tail, src.Size-int64(len(tail))); err != nil {
		return err
	}
	i := bytes.LastIndex(tail[:len(tail)-endSize+4], []byte("PK\x05\x06"))
	if i < 0 {
		return zip.ErrFormat
	}
	end := tail[i:]
	le := binary.LittleEndian
	if int(le.Uint16(end[20:]))+endSize != len(end) {
		return zip.ErrFormat
	}
	endOffset := uint64(src.Size - int64(len(end)))
	count := uint64(le.Uint16(end[10:]))
	size := uint64(le.Uint32(end[12:]))
	offset := uint64(le.Uint32(end[16:]))
	if count == 0xffff || size == 0xffff || size == 0xffffffff || offset == 0xffffffff {
		var locator [20]byte
		if endOffset < 20 {
			return zip.ErrFormat
		}
		if _, err := src.R.ReadAt(locator[:], int64(endOffset)-20); err != nil {
			return err
		}
		if string(locator[:4]) == "PK\x06\x07" {
			z64 := le.Uint64(locator[8:])
			if endOffset < 76 || z64 > endOffset-76 || le.Uint32(locator[4:]) != 0 || le.Uint32(locator[16:]) != 1 {
				return zip.ErrFormat
			}
			var record [56]byte
			if _, err := src.R.ReadAt(record[:], int64(z64)); err != nil {
				return err
			}
			if string(record[:4]) != "PK\x06\x06" {
				return zip.ErrFormat
			}
			endOffset = z64
			count, size, offset = le.Uint64(record[32:]), le.Uint64(record[40:]), le.Uint64(record[48:])
		}
	}
	if count > uint64(lim.MaxEntries) {
		return fmt.Errorf("%w: more than %d archive entries", ErrTooLarge, lim.MaxEntries)
	}
	if offset > endOffset || size != endOffset-offset {
		return zip.ErrFormat
	}
	const maxDirectoryBytes = 64 << 20
	if size > maxDirectoryBytes {
		return fmt.Errorf("%w: archive directory exceeds 64 MiB", ErrTooLarge)
	}
	r := bufio.NewReader(io.NewSectionReader(src.R, int64(offset), int64(size)))
	var header [46]byte
	for range count {
		if _, err := io.ReadFull(r, header[:]); err != nil || string(header[:4]) != "PK\x01\x02" {
			return zip.ErrFormat
		}
		extra := int(le.Uint16(header[28:])) + int(le.Uint16(header[30:])) + int(le.Uint16(header[32:]))
		if _, err := r.Discard(extra); err != nil {
			return zip.ErrFormat
		}
	}
	if _, err := r.ReadByte(); err != io.EOF {
		return zip.ErrFormat
	}
	return nil
}
