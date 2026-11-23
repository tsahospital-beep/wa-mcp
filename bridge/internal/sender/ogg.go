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
	count := 0
	seconds, err := readOpus(data, func(int) { count++ })
	if err != nil {
		return VoiceInfo{}, err
	}
	if count == 0 {
		return VoiceInfo{Seconds: seconds, Waveform: make([]byte, waveformBars)}, nil
	}

	// Accumulate into fixed buckets. A page may contain many very short packets.
	var averages [waveformBars]float64
	index := 0
	_, err = readOpus(data, func(size int) {
		bar := index
		if count >= waveformBars {
			// Last bucket whose start (bar * count / waveformBars) is at or before index.
			bar = int(((int64(index)+1)*waveformBars - 1) / int64(count))
		}
		averages[bar] += float64(size)
		index++
	})
	if err != nil {
		return VoiceInfo{}, err
	}
	if count < waveformBars {
		// Repeat short clips' packet sizes into the unused bars, working back to front.
		for bar := waveformBars - 1; bar >= 0; bar-- {
			averages[bar] = averages[bar*count/waveformBars]
		}
	} else {
		for bar := range averages {
			from := int64(bar) * int64(count) / waveformBars
			to := int64(bar+1) * int64(count) / waveformBars
			averages[bar] /= float64(to - from)
		}
	}
	return VoiceInfo{Seconds: seconds, Waveform: waveform(averages)}, nil
}

func readOpus(data []byte, packet func(int)) (uint32, error) {
	var (
		serial   uint32
		haveHead bool
		haveTags bool
		preSkip  uint64
		granule  int64 = -1
		pending  int   // bytes of a packet that continues on the next page
	)
	for pos := 0; len(data)-pos >= pageHeader && bytes.Equal(data[pos:pos+4], []byte("OggS")); {
		hdr := data[pos : pos+pageHeader]
		if hdr[4] != 0 {
			return 0, ErrNotOpus
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
					return 0, ErrNotOpus
				}
				preSkip = uint64(binary.LittleEndian.Uint16(data[start+10 : start+12]))
				haveHead = true
			case !haveTags:
				haveTags = true
			default:
				packet(pending)
			}
			pending = 0
		}
		if pageGranule >= 0 && haveTags {
			granule = pageGranule
		}
		pos = next
	}
	if !haveHead {
		return 0, ErrNotOpus
	}
	if granule <= 0 || uint64(granule) <= preSkip {
		return 1, nil
	}
	seconds := (uint64(granule) - preSkip + opusRate/2) / opusRate
	if seconds > math.MaxUint32 {
		return 0, ErrNotOpus
	}
	return max(1, uint32(seconds)), nil
}

// waveform scales the average packet sizes to 5..100.
func waveform(avg [waveformBars]float64) []byte {
	out := make([]byte, waveformBars)
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
