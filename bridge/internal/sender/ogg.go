package sender

import (
	"bytes"
	"encoding/binary"
	"errors"
	"math"
)

// VoiceInfo is what a voice note message needs besides the file itself.
type VoiceInfo struct {
	Seconds  uint32
	Waveform []byte // 64 values in 0..100
}

var ErrNotOpus = errors.New("voice notes must be Ogg/Opus files")

const (
	waveformBars = 64
	opusRate     = 48000 // Opus granule positions always count 48 kHz samples
	pageHeader   = 27
)

// ParseOpus reads an Ogg/Opus file. The duration is the last granule position
// minus the pre-skip; the waveform comes from audio packet sizes (louder,
// busier audio needs more bytes per packet).
func ParseOpus(data []byte) (VoiceInfo, error) {
	var (
		serial   uint32
		haveHead bool
		haveTags bool
		preSkip  uint64
		granule  int64 = -1
		pending  int   // bytes of a packet that continues on the next page
		packets  []int
	)
	for pos := 0; len(data)-pos >= pageHeader && bytes.Equal(data[pos:pos+4], []byte("OggS")); {
		hdr := data[pos : pos+pageHeader]
		if hdr[4] != 0 {
			return VoiceInfo{}, ErrNotOpus
		}
		pageGranule := int64(binary.LittleEndian.Uint64(hdr[6:14]))
		pageSerial := binary.LittleEndian.Uint32(hdr[14:18])
		nseg := int(hdr[26])
		if len(data)-pos < pageHeader+nseg {
			break
		}
		lacing := data[pos+pageHeader : pos+pageHeader+nseg]
		body := pos + pageHeader + nseg
		size := 0
		for _, l := range lacing {
			size += int(l)
		}
		if len(data)-body < size {
			break
		}
		next := body + size
		if !haveHead {
			serial = pageSerial
		}
		if pageSerial != serial {
			pos = next
			continue
		}

		end := body
		for _, l := range lacing {
			pending += int(l)
			end += int(l)
			if l == 255 {
				continue
			}
			switch {
			case !haveHead:
				start := end - pending
				if start < body || pending < 19 || !bytes.Equal(data[start:start+8], []byte("OpusHead")) {
					return VoiceInfo{}, ErrNotOpus
				}
				preSkip = uint64(binary.LittleEndian.Uint16(data[start+10 : start+12]))
				haveHead = true
			case !haveTags:
				haveTags = true
			default:
				packets = append(packets, pending)
			}
			pending = 0
		}
		if pageGranule >= 0 && haveTags {
			granule = pageGranule
		}
		pos = next
	}
	if !haveHead {
		return VoiceInfo{}, ErrNotOpus
	}
	info := VoiceInfo{Waveform: waveform(packets)}
	if granule > 0 && uint64(granule) > preSkip {
		info.Seconds = uint32(math.Round(float64(uint64(granule)-preSkip) / opusRate))
	}
	if info.Seconds == 0 {
		info.Seconds = 1
	}
	return info, nil
}

// waveform averages packet sizes into 64 bars scaled to 5..100.
func waveform(packets []int) []byte {
	out := make([]byte, waveformBars)
	if len(packets) == 0 {
		return out
	}
	avg := make([]float64, waveformBars)
	for i := range avg {
		from := i * len(packets) / waveformBars
		to := (i + 1) * len(packets) / waveformBars
		if from >= len(packets) {
			from = len(packets) - 1
		}
		if to <= from {
			to = from + 1
		}
		sum := 0
		for _, p := range packets[from:to] {
			sum += p
		}
		avg[i] = float64(sum) / float64(to-from)
	}
	lo, hi := avg[0], avg[0]
	for _, v := range avg {
		lo = math.Min(lo, v)
		hi = math.Max(hi, v)
	}
	for i, v := range avg {
		level := 50.0
		if hi > lo {
			level = 5 + 95*(v-lo)/(hi-lo)
		}
		out[i] = byte(math.Round(level))
	}
	return out
}
