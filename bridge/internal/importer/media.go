package importer

import (
	"path"
	"regexp"
	"strings"

	"github.com/tsahospital-beep/wa-mcp/bridge/internal/common"
	"github.com/tsahospital-beep/wa-mcp/bridge/internal/store"
)

// omittedPhrase is a media placeholder an export writes in place of a file.
type omittedPhrase struct {
	text string
	typ  string // "media" when the type is not named
}

// defaultOmitted lists placeholders in English and a few common languages.
// Non-ASCII letters are written as escapes. Add more with import.omitted_phrases.
var defaultOmitted = []omittedPhrase{
	{"<Media omitted>", "media"},
	{"image omitted", "image"},
	{"video omitted", "video"},
	{"audio omitted", "audio"},
	{"voice message omitted", "voice"},
	{"sticker omitted", "sticker"},
	{"GIF omitted", "image"},
	{"document omitted", "document"},
	{"Contact card omitted", "contact"},
	// Spanish
	{"<Multimedia omitido>", "media"},
	{"imagen omitida", "image"},
	{"v\u00eddeo omitido", "video"},
	{"audio omitido", "audio"},
	{"sticker omitido", "sticker"},
	{"documento omitido", "document"},
	// Portuguese
	{"<M\u00eddia oculta>", "media"},
	{"imagem omitida", "image"},
	{"\u00e1udio omitido", "audio"},
	{"figurinha omitida", "sticker"},
	{"documento omitido", "document"},
	// German
	{"<Medien ausgeschlossen>", "media"},
	{"Bild weggelassen", "image"},
	{"Video weggelassen", "video"},
	{"Audio weggelassen", "audio"},
	{"Sticker weggelassen", "sticker"},
	{"Dokument weggelassen", "document"},
	// French
	{"<M\u00e9dias omis>", "media"},
	{"image omise", "image"},
	{"vid\u00e9o omise", "video"},
	{"audio omis", "audio"},
	{"sticker omis", "sticker"},
	{"document omis", "document"},
	// Italian
	{"<Media omessi>", "media"},
	{"immagine omessa", "image"},
	{"video omesso", "video"},
	{"audio omesso", "audio"},
	{"sticker omesso", "sticker"},
	{"documento omesso", "document"},
	// Dutch, Indonesian, Turkish, Russian
	{"<Media weggelaten>", "media"},
	{"<Media tidak disertakan>", "media"},
	{"<Medya dahil edilmedi>", "media"},
	{"<\u0411\u0435\u0437 \u043c\u0435\u0434\u0438\u0430\u0444\u0430\u0439\u043b\u043e\u0432>", "media"},
}

// omittedSet matches whole lines that are media placeholders.
type omittedSet struct{ byText map[string]string }

func newOmittedSet(extra []string) *omittedSet {
	o := &omittedSet{byText: map[string]string{}}
	for _, p := range defaultOmitted {
		o.byText[normPhrase(p.text)] = p.typ
	}
	for _, e := range extra {
		if k := normPhrase(e); k != "" {
			o.byText[k] = "media"
		}
	}
	return o
}

// normPhrase folds case and drops marks, angle brackets and extra spaces.
func normPhrase(s string) string {
	s = trimMarks(strings.TrimSpace(s))
	s = strings.TrimSuffix(strings.TrimPrefix(s, "<"), ">")
	return store.Fold(strings.Join(strings.Fields(s), " "))
}

// match reports whether the line is a placeholder and which kind of media it stands for.
func (o *omittedSet) match(line string) (string, bool) {
	if o == nil {
		return "", false
	}
	typ, ok := o.byText[normPhrase(line)]
	return typ, ok
}

var (
	// iosAttach is "<attached: 00000012-PHOTO-2024-01-01-10-00-00.jpg>" in any language.
	iosAttach = regexp.MustCompile(`^<[^<>:]{2,30}[ \x{00a0}]?:[ \x{00a0}]*([^<>]+\.[A-Za-z0-9]{1,8})>$`)
	// androidAttach is "IMG-20240101-WA0001.jpg (file attached)".
	androidAttach = regexp.MustCompile(`^(.+\.[A-Za-z0-9]{1,8}) \(([^()]{2,40})\)$`)
)

// attachedLabels are the folded words of Android attachment lines. A line with
// another label still counts when the archive holds a file of that name.
var attachedLabels = []string{
	"file attached", "archivo adjunto", "datei angeh\u00e4ngt", "fichier joint", "arquivo anexado",
	"file allegato", "bestand bijgevoegd", "dosya eklendi", "file terlampir",
}

func attachedSet() map[string]bool {
	m := map[string]bool{}
	for _, l := range attachedLabels {
		m[store.Fold(l)] = true
	}
	return m
}

// attachmentLine returns the file name of an attachment line.
func attachmentLine(line string, o parseOptions) (string, bool) {
	l := trimMarks(strings.TrimSpace(line))
	if m := iosAttach.FindStringSubmatch(l); m != nil {
		return baseName(m[1]), true
	}
	if m := androidAttach.FindStringSubmatch(l); m != nil {
		name := baseName(m[1])
		if o.Attached[store.Fold(strings.TrimSpace(m[2]))] || (o.HasFile != nil && o.HasFile(name)) {
			return name, true
		}
	}
	return "", false
}

// baseName keeps the last part of a name written with either separator.
func baseName(s string) string {
	s = strings.TrimSpace(strings.ReplaceAll(s, "\\", "/"))
	return path.Base(s)
}

// fileKind gives the message kind and the MIME type of an attachment from its name:
// the extension decides, the prefixes WhatsApp uses (STK-, PTT-) refine it.
func fileKind(name string) (kind, mime string) {
	ext := strings.ToLower(path.Ext(name))
	mime = common.MediaMIME(ext, false)
	upper := strings.ToUpper(name)
	switch {
	case ext == ".webp" && (strings.HasPrefix(upper, "STK-") || strings.Contains(upper, "-STICKER-")):
		return store.KindSticker, mime
	case ext == ".opus" || strings.HasPrefix(upper, "PTT-"):
		return store.KindVoice, mime
	case strings.HasPrefix(mime, "image/"):
		return store.KindImage, mime
	case strings.HasPrefix(mime, "video/"):
		return store.KindVideo, mime
	case strings.HasPrefix(mime, "audio/"):
		return store.KindAudio, mime
	}
	return store.KindDocument, mime
}
