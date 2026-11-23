package sender

import (
	"errors"
	"fmt"
	"strings"
	"unicode"
	"unicode/utf8"
)

const messageChars = 4096
const totalChars = 12000

var ErrTextLimit = errors.New("text exceeds the total limit of 12000 characters")

// SplitText preserves whitespace and counts Unicode code points, including mention tags.
func SplitText(text string) ([]string, error) {
	n := utf8.RuneCountInString(text)
	if n > totalChars {
		return nil, ErrTextLimit
	}
	if n <= messageChars {
		return []string{text}, nil
	}
	// At this total limit each marker fits in six characters.
	const room = messageChars - len("(1/2) ")
	runes := []rune(text)
	var parts []string
	for len(runes) > room {
		cut := textBoundary(runes[:room])
		parts = append(parts, string(runes[:cut]))
		runes = runes[cut:]
	}
	parts = append(parts, string(runes))
	for i := range parts {
		parts[i] = fmt.Sprintf("(%d/%d) %s", i+1, len(parts), parts[i])
	}
	return parts, nil
}

func textBoundary(window []rune) int {
	paragraph, sentence, word := 0, 0, 0
	for i, r := range window {
		if strings.ContainsRune("\u3002\uff01\uff1f", r) {
			sentence = i + 1
		}
		if !unicode.IsSpace(r) {
			continue
		}
		word = i + 1
		if r == '\n' {
			paragraph = word
		}
		if i > 0 && strings.ContainsRune(".!?", window[i-1]) {
			sentence = word
		}
	}
	if paragraph >= len(window)/2 {
		return paragraph
	}
	if boundary := max(paragraph, sentence); boundary > 0 {
		return boundary
	}
	if word > 0 {
		return word
	}
	return len(window)
}
