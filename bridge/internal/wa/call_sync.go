package wa

import (
	"context"

	"go.mau.fi/whatsmeow/proto/waSyncAction"
	"go.mau.fi/whatsmeow/types"

	"github.com/tsahospital-beep/wa-mcp/bridge/internal/store"
)

func (in *Ingest) syncedCall(ctx context.Context, r *waSyncAction.CallLogRecord) {
	if !in.RecordCalls || r.GetCallID() == "" || r.GetStartTime() <= 0 {
		return
	}
	creator, _ := types.ParseJID(r.GetCallCreatorJID())
	outgoing := !r.GetIsIncoming()
	if r.IsIncoming == nil {
		outgoing = in.isSelf(creator)
	}
	peer := creator.ToNonAD()
	if outgoing {
		peer = types.EmptyJID
		for _, participant := range r.GetParticipants() {
			j, _ := types.ParseJID(participant.GetUserJID())
			if j.User != "" && !in.isSelf(j) {
				peer = j.ToNonAD()
				break
			}
		}
	}
	group, _ := types.ParseJID(r.GetGroupJID())
	if peer.IsEmpty() {
		peer = group
	}
	call := store.Call{
		ID: r.GetCallID(), Peer: peer.String(), Group: group.ToNonAD().String(),
		Outgoing: outgoing, Video: r.GetIsVideo(), StartedAt: r.GetStartTime(),
	}
	if r.CallResult != nil {
		switch r.GetCallResult() {
		case waSyncAction.CallLogRecord_CONNECTED, waSyncAction.CallLogRecord_ACCEPTEDELSEWHERE:
			call.Outcome = store.CallAnswered
		case waSyncAction.CallLogRecord_REJECTED:
			call.Outcome = store.CallRejected
		case waSyncAction.CallLogRecord_MISSED:
			call.Outcome = store.CallMissed
		}
	}
	if r.GetDuration() > 0 {
		call.EndedAt = call.StartedAt + r.GetDuration()
	}
	if err := in.DB.NoteCall(ctx, call); err != nil {
		in.logf("store synced call: %v", err)
	}
}
