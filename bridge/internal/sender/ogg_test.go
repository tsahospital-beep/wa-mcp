package sender

import (
	"bytes"
	"encoding/binary"
	"errors"
	"testing"
)

// oggPage builds one Ogg page (CRC left at zero; the parser does not check it).
func oggPage(serial, seq uint32, granule int64, flags byte, segments []byte, payload []byte) []byte {
	var b bytes.Buffer
	b.WriteString("OggS")
	b.WriteByte(0)
	b.WriteByte(flags)
	binary.Write(&b, binary.LittleEndian, granule)
	binary.Write(&b, binary.LittleEndian, serial)
	binary.Write(&b, binary.LittleEndian, seq)
	binary.Write(&b, binary.LittleEndian, uint32(0))
	b.WriteByte(byte(len(segments)))
	b.Write(segments)
	b.Write(payload)
	return b.Bytes()
}

func lacing(sizes ...int) []byte {
	var out []byte
	for _, n := range sizes {
		for n >= 255 {
			out = append(out, 255)
			n -= 255
		}
		out = append(out, byte(n))
	}
	return out
}

func packets(sizes ...int) []byte {
	var out []byte
	for i, n := range sizes {
		out = append(out, bytes.Repeat([]byte{byte(i + 1)}, n)...)
	}
	return out
}

// syntheticOpus is a 3.4 s mono Opus stream: header pages, three audio pages
// and one packet that continues across a page boundary.
func syntheticOpus() []byte {
	const serial = 0x1234
	const preSkip = 312
	head := []byte("OpusHead")
	head = append(head, 1, 1)
	head = binary.LittleEndian.AppendUint16(head, preSkip)
	head = binary.LittleEndian.AppendUint32(head, 48000)
	head = append(head, 0, 0, 0)
	tags := []byte("OpusTags")
	tags = binary.LittleEndian.AppendUint32(tags, 4)
	tags = append(tags, "test"...)
	tags = binary.LittleEndian.AppendUint32(tags, 0)

	var file []byte
	file = append(file, oggPage(serial, 0, 0, 0x02, lacing(len(head)), head)...)
	file = append(file, oggPage(serial, 1, 0, 0, lacing(len(tags)), tags)...)
	// quiet start: small packets
	file = append(file, oggPage(serial, 2, 48000, 0, lacing(3, 3, 3, 3), packets(3, 3, 3, 3))...)
	// loud middle: a 300-byte packet split as 255 here + 45 on the next page
	file = append(file, oggPage(serial, 3, -1, 0, []byte{120, 255}, packets(120, 255))...)
	file = append(file, oggPage(serial, 4, 96000, 0x01, lacing(45, 160), packets(45, 160))...)
	// end
	file = append(file, oggPage(serial, 5, preSkip+163200, 0x04, lacing(20, 10), packets(20, 10))...)
	return file
}

func TestParseOpus(t *testing.T) {
	info, err := ParseOpus(syntheticOpus())
	if err != nil {
		t.Fatal(err)
	}
	if info.Seconds != 3 {
		t.Fatalf("seconds = %d, want 3 (163200 samples at 48 kHz)", info.Seconds)
	}
	if len(info.Waveform) != 64 {
		t.Fatalf("waveform length %d", len(info.Waveform))
	}
	lo, hi := byte(255), byte(0)
	for _, v := range info.Waveform {
		if v > 100 {
			t.Fatalf("waveform value %d > 100", v)
		}
		lo, hi = min(lo, v), max(hi, v)
	}
	if lo != 5 || hi != 100 {
		t.Fatalf("waveform range %d..%d, want 5..100", lo, hi)
	}
	// The loud middle is higher than the quiet start.
	if info.Waveform[0] >= info.Waveform[30] {
		t.Fatalf("waveform shape: start %d, middle %d", info.Waveform[0], info.Waveform[30])
	}
}

func TestParseOpusRejects(t *testing.T) {
	data := syntheticOpus()
	last := bytes.LastIndex(data, []byte("OggS"))
	binary.LittleEndian.PutUint64(data[last+6:], 1<<62)
	if _, err := ParseOpus(data); !errors.Is(err, ErrNotOpus) {
		t.Fatalf("unrepresentable duration: %v", err)
	}
	if _, err := ParseOpus([]byte("ID3 not an ogg file")); !errors.Is(err, ErrNotOpus) {
		t.Fatalf("plain bytes: %v", err)
	}
	vorbis := append([]byte{1}, []byte("vorbis-----------------------")...)
	if _, err := ParseOpus(oggPage(1, 0, 0, 0x02, lacing(len(vorbis)), vorbis)); !errors.Is(err, ErrNotOpus) {
		t.Fatalf("vorbis: %v", err)
	}
	if _, err := ParseOpus(nil); !errors.Is(err, ErrNotOpus) {
		t.Fatalf("empty: %v", err)
	}
}

func TestParseOpusMinimumOneSecond(t *testing.T) {
	head := append([]byte("OpusHead"), 1, 1, 0, 0, 0x80, 0xbb, 0, 0, 0, 0, 0)
	data := oggPage(7, 0, 0, 0x02, lacing(len(head)), head)
	info, err := ParseOpus(data)
	if err != nil || info.Seconds != 1 {
		t.Fatalf("%+v %v", info, err)
	}
}

func TestLongWaveformUsesAllPackets(t *testing.T) {
	head := append([]byte("OpusHead"), make([]byte, 11)...)
	data := oggPage(1, 0, 0, 2, lacing(len(head)), head)
	data = append(data, oggPage(1, 1, 0, 0, lacing(8), []byte("OpusTags"))...)
	var sizes []int
	for i := 0; i < 130; i++ {
		sizes = append(sizes, i+1)
	}
	data = append(data, oggPage(1, 2, 48000, 4, lacing(sizes...), packets(sizes...))...)
	got, err := ParseOpus(data)
	if err != nil {
		t.Fatal(err)
	}
	var avg [waveformBars]float64
	for i := range avg {
		group := sizes[i*len(sizes)/waveformBars : (i+1)*len(sizes)/waveformBars]
		for _, size := range group {
			avg[i] += float64(size) / float64(len(group))
		}
	}
	if !bytes.Equal(got.Waveform, waveform(avg)) {
		t.Fatal("waveform does not match the packet averages")
	}
}
