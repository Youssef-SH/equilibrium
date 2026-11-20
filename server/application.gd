extends RefCounted
## One lobby/match. Transport identities are supplied by the socket owner only.
const Protocol = preload("res://shared/protocol_v1.gd")
const Sessions = preload("res://server/session_registry.gd")
const MatchSession = preload("res://server/match_session.gd")
const Presentation = preload("res://server/presentation.gd")
var presentation = Presentation.new()
const Bots = preload("res://server/bot_controller.gd")
var sessions = Sessions.new()
var connections: Dictionary = {}
var seats: Array = [null,null,null,null]
var host: int = -1
var lobby_id: String = ""
var join_code: String = ""
var lobby_expires: int = 0
var revision: int = 0
var phase: String = "waiting"
var match_session: RefCounted
var seed_value: int = 42
var outbox: Array = []
var disconnect_ids: Array = []
var draft_disconnect_deadlines: Dictionary = {}
var draft_grace_ms: int = 30000

func connect_peer(id: int) -> void:
	connections[id] = false
func emit(id: int, kind: String, body: Dictionary = {}) -> void:
	if connections.has(id): outbox.append({"connection":id,"message":Protocol.message(kind,body)})
func reject(id: int, m: Dictionary, code: String) -> void:
	emit(id,"protocol_error",{"correlation_id":m.get("correlation_id",""),"code":code,"fatal":false})
func request(id: int, m: Dictionary, now: int) -> void:
	# Expiry is enforced before any request can authenticate or mutate the draft.
	# This also protects direct application users that do not use the socket loop.
	enforce_draft_deadlines(now)
	if not connections.has(id): return
	var error: String = Protocol.validate(m)
	if error != "": reject(id,m,error); return
	if m.type == "hello":
		if connections[id]: reject(id,m,"already_negotiated"); return
		connections[id] = true
		emit(id,"welcome",{"correlation_id":m.correlation_id,"connection_id":str(id),"server_version":"1.0","limits":{"message_bytes":32768,"messages_per_second":20}})
		return
	if not connections[id]: reject(id,m,"hello_required"); return
	var seat: int = sessions.seat_for(id)
	if m.type in ["create_lobby","join_lobby","rejoin"]:
		if seat >= 0: reject(id,m,"already_seated"); return
		if m.type == "rejoin":
			seat = sessions.authenticate(m.lobby_id,m.rejoin_token,now)
			if seat < 0: reject(id,m,"authentication_failed"); return
			var old_id: int = sessions.entries[seat].connection
			if old_id >= 0:
				disconnect_ids.append(old_id)
				connections.erase(old_id)
			sessions.entries[seat].connection = id
			draft_disconnect_deadlines.erase(seat)
			var secret: String = sessions.rotate(seat,now)
			emit(id,"seat_assigned",{"correlation_id":m.correlation_id,"lobby_id":lobby_id,"seat":seat,"host":seat==host,"rejoin_token":secret,"generation":sessions.entries[seat].generation})
			revision += 1
			broadcast_lobby()
			if phase == "draft": send_to_seat(seat,match_session.draft_view())
			elif phase in ["running","finished","failed"]:
				send_to_seat(seat,match_session.view(seat))
			# Separate messages keep acknowledgment recovery bounded per frame.
			if match_session != null:
				for record in match_session.gateway.records[seat].values().slice(-64):
					for message in record.acks: send_to_seat(seat,message)
			return
		if phase != "waiting": reject(id,m,"lobby_started"); return
		if m.type == "create_lobby":
			if lobby_id != "": reject(id,m,"lobby_exists"); return
			lobby_id = sessions.token().substr(0,24)
			join_code = sessions.token().substr(0,16)
			lobby_expires = now+1800000
		elif lobby_id == "" or now >= lobby_expires or m.join_code != join_code:
			reject(id,m,"invalid_join_code"); return
		seat = seats.find(null)
		if seat < 0: reject(id,m,"lobby_full"); return
		seats[seat] = {"controller":"human","ready":false}
		if host < 0: host = seat
		var secret: String = sessions.create(seat,id,lobby_id,now)
		emit(id,"seat_assigned",{"correlation_id":m.correlation_id,"lobby_id":lobby_id,"seat":seat,"host":seat==host,"rejoin_token":secret,"generation":1})
		if seat == host: emit(id,"lobby_created",{"correlation_id":m.correlation_id,"lobby_id":lobby_id,"join_code":join_code})
		revision += 1
		broadcast_lobby()
		return
	if seat < 0: reject(id,m,"not_seated"); return
	match m.type:
		"get_catalogue":
			var catalogue: Dictionary = presentation.catalogue()
			catalogue.correlation_id = m.correlation_id
			emit(id,"catalogue",catalogue)
		"preview_action":
			if phase != "running" or match_session == null: reject(id,m,"invalid_phase"); return
			var preview: Dictionary = presentation.preview(match_session.game.engine,seat,m.kind,m.payload)
			preview.correlation_id = m.correlation_id
			emit(id,"action_preview",preview)
		"set_ready":
			if phase != "waiting": reject(id,m,"invalid_phase"); return
			if seats[seat].ready != m.ready:
				seats[seat].ready = m.ready
				revision += 1
			broadcast_lobby()
		"start_match":
			if phase != "waiting": reject(id,m,"invalid_phase"); return
			if seat != host: reject(id,m,"host_only"); return
			for s in seats:
				if s != null and not s.ready: reject(id,m,"not_ready"); return
			var bots: Array = []
			for i in range(4):
				if seats[i] == null:
					seats[i] = {"controller":"bot","ready":true}
					bots.append(i)
			match_session = MatchSession.new()
			error = match_session.initialize("match-"+lobby_id,seed_value,bots)
			if error != "": phase = "failed"; reject(id,m,"initialization_failed"); return
			phase = "draft"
			revision += 1
			broadcast_lobby()
			publish_draft(now)
		"choose_foundation", "submit_action":
			if match_session == null: reject(id,m,"invalid_phase"); return
			# Replay previous receipts even after phase transition or completion.
			var prior: bool = match_session.gateway.records[seat].has(m.action_id)
			if not prior and ((m.type == "choose_foundation" and phase != "draft") or (m.type == "submit_action" and phase != "running")):
				reject(id,m,"invalid_phase"); return
			var kind: String = "select_foundation" if m.type == "choose_foundation" else m.kind
			var payload: Dictionary = {"foundation":m.foundation} if m.type == "choose_foundation" else m.payload
			for response in match_session.gateway.issue(seat,m.action_id,kind,payload):
				response.correlation_id = m.correlation_id
				send_to_seat(seat,response)
			if phase == "draft":
				Bots.draft(match_session.game,match_session.gateway,match_session.bots)
				publish_draft(now)
		_: reject(id,m,"invalid_phase")

func send_to_seat(seat: int, message: Dictionary) -> void:
	if sessions.entries.has(seat):
		var id: int = sessions.entries[seat].connection
		if connections.has(id): outbox.append({"connection":id,"message":message.duplicate(true)})
func broadcast(message: Dictionary) -> void:
	for seat in sessions.entries: send_to_seat(seat,message)
func broadcast_lobby() -> void:
	var rows: Array = []
	for i in range(4):
		var row: Dictionary = {"seat":i,"controller":"open","ready":false,"connected":false}
		if seats[i] != null:
			row.merge(seats[i],true)
			row.connected = seats[i].controller == "bot" or (sessions.entries.has(i) and sessions.entries[i].connection >= 0)
		rows.append(row)
	broadcast(Protocol.message("lobby_state",{"lobby_id":lobby_id,"lobby_rev":revision,"host_seat":host,"phase":phase,"seats":rows}))
func publish_draft(now: int) -> void:
	broadcast(match_session.draft_view())
	if match_session.game.engine != null:
		phase = "running"
		revision += 1
		broadcast_lobby()
		match_session.scheduler.start(now)
		broadcast(Protocol.message("match_started",{"match_id":match_session.game.draft.match_id,"tick":0}))
		publish_views()
func publish_views() -> void:
	for seat in sessions.entries: send_to_seat(seat,match_session.view(seat))
func enforce_draft_deadlines(now: int) -> bool:
	if phase != "draft": return false
	var expired: Array = []
	for seat in draft_disconnect_deadlines.keys():
		if now >= int(draft_disconnect_deadlines[seat]): expired.append(seat)
	if expired.is_empty(): return false
	expired.sort()
	cancel_draft(int(expired[0]))
	return true
func advance(now: int) -> void:
	if phase == "draft":
		enforce_draft_deadlines(now)
		return
	if phase != "running": return
	for _i in range(match_session.scheduler.due(now)):
		var result: Dictionary = match_session.step()
		if result.status != "advanced":
			phase = "failed"
			broadcast(Protocol.message("protocol_error",{"code":"simulation_failed","fatal":true}))
			return
		for delivery in result.deliveries: send_to_seat(delivery.seat,delivery.message)
		publish_views()
		if match_session.game.snapshot().status == "finished":
			phase = "finished"
			broadcast(Protocol.message("match_finished",match_session.view(0).final_result))
			return
func disconnect_peer(id: int, now: int = -1) -> void:
	connections.erase(id)
	var seat: int = sessions.seat_for(id)
	if seat < 0: return
	if phase == "waiting":
		sessions.entries.erase(seat)
		seats[seat] = null
		if seat == host:
			host = -1
			for i in range(4):
				if seats[i] != null: host = i; break
		if host < 0:
			lobby_id = ""
			join_code = ""
	else:
		sessions.entries[seat].connection = -1
		if phase == "draft":
			var disconnected_at: int = Time.get_ticks_msec() if now < 0 else now
			draft_disconnect_deadlines[seat] = disconnected_at+draft_grace_ms
	revision += 1
	broadcast_lobby()

func cancel_draft(seat: int) -> void:
	# Notify before invalidating every session. The same connections may create a
	# fresh lobby after cancellation, but no stale draft action can be admitted.
	for entry in sessions.entries.values():
		var id: int = entry.connection
		if connections.has(id): emit(id,"match_cancelled",{"reason":"draft_rejoin_timeout","seat":seat})
	sessions.entries.clear()
	seats = [null,null,null,null]
	host = -1
	lobby_id = ""
	join_code = ""
	lobby_expires = 0
	match_session = null
	draft_disconnect_deadlines.clear()
	phase = "waiting"
	revision += 1
