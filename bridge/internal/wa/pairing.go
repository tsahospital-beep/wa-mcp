package wa

import (
	"fmt"

	"go.mau.fi/whatsmeow"
	"go.mau.fi/whatsmeow/proto/waCompanionReg"
	"go.mau.fi/whatsmeow/proto/waWa6"
	waStore "go.mau.fi/whatsmeow/store"
	"google.golang.org/protobuf/proto"
)

type pairingIdentity struct {
	platform   waCompanionReg.DeviceProps_PlatformType
	clientType whatsmeow.PairClientType
	browser    string
	props      []byte
}

func pairingFor(opts Options) (pairingIdentity, error) {
	// Phone-code requests require a recognized browser and OS in the prompt.
	id := pairingIdentity{platform: waCompanionReg.DeviceProps_DESKTOP, clientType: whatsmeow.PairClientOtherWebClient, browser: "Firefox"}
	switch opts.DevicePlatform {
	case "", "desktop":
	case "chrome":
		id.platform, id.clientType, id.browser = waCompanionReg.DeviceProps_CHROME, whatsmeow.PairClientChrome, "Chrome"
	case "firefox":
		id.platform, id.clientType = waCompanionReg.DeviceProps_FIREFOX, whatsmeow.PairClientFirefox
	case "edge":
		id.platform, id.clientType, id.browser = waCompanionReg.DeviceProps_EDGE, whatsmeow.PairClientEdge, "Edge"
	case "safari":
		id.platform, id.clientType, id.browser = waCompanionReg.DeviceProps_SAFARI, whatsmeow.PairClientSafari, "Safari"
	case "unknown":
		id.platform, id.clientType = waCompanionReg.DeviceProps_UNKNOWN, whatsmeow.PairClientUnknown
	default:
		return id, fmt.Errorf("invalid login.device_platform %q", opts.DevicePlatform)
	}
	name := opts.DeviceName
	if name == "" {
		name = "WA MCP"
	}
	props := proto.CloneOf(waStore.DeviceProps)
	props.Os = proto.String(name)
	props.PlatformType = id.platform.Enum()
	data, err := proto.Marshal(props)
	id.props = data
	return id, err
}

func (id pairingIdentity) configure(wm *whatsmeow.Client) {
	wm.QRClientType = id.clientType
	wm.GetClientPayload = func() *waWa6.ClientPayload {
		payload := wm.Store.GetClientPayload()
		if registration := payload.GetDevicePairingData(); registration != nil {
			registration.DeviceProps = id.props
		}
		return payload
	}
}
