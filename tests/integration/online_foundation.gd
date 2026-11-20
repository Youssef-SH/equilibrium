extends SceneTree
const Protocol = preload("res://shared/protocol_v1.gd")
const App = preload("res://server/application.gd")
const Scheduler = preload("res://server/tick_scheduler.gd")
const View = preload("res://simulation/projections/player_view.gd")
var checks: int = 0
var failures: int = 0
func check(ok: bool, label: String) -> void:
	checks += 1
	if not ok: failures += 1; printerr("FAIL: "+label)
func message(kind: String, body: Dictionary = {}) -> Dictionary:
	body.correlation_id = "test"
	return Protocol.message(kind,body)
func _initialize() -> void:
	var good: String = '{"v":1,"type":"hello","correlation_id":"x","client":"test","client_version":"1"}'
	check(Protocol.decode(good.to_utf8_buffer()).error == "","valid hello")
	for bad in ['{"v":1,"v":1}', '{"v":1,}', '{"x":[1,]}','{"x":01}','{"x":1.0}','{"x":1e0}','{"x":NaN}','{"x":9007199254740992}','{"x":-9007199254740992}','{"x":"\\uD800"}','{"x":"\\uDC00"}','{"x":"a\nb"}','{} garbage','[]','true','{"x":/*x*/1}','{"v":1,"\\u0076":2}']:
		check(Protocol.decode(bad.to_utf8_buffer()).error != "","strict JSON "+bad)
	check(Protocol.Decoder.new().decode('{"x":"\\uD83D\\uDE00"}'.to_utf8_buffer()).error == "","valid surrogate pair")
	check(Protocol.decode((' '.repeat(32769)).to_utf8_buffer()).error == "message_too_large","size")
	check(Protocol.decode(PackedByteArray([255])).error == "invalid_utf8","utf8")
	for bad in ['{"x":'+ '['.repeat(10)+'0'+']'.repeat(10)+'}', '{"x":"'+'a'.repeat(1025)+'"}', '{"x":['+'0,'.repeat(64)+'0]}']:
		check(Protocol.decode(bad.to_utf8_buffer()).error != "","bounded containers")
	for patch in [{"v":2},{"type":"advance"},{"seat":1},{"correlation_id":1}]:
		var m: Dictionary = message("start_match")
		m.merge(patch,true)
		check(Protocol.validate(m) != "","invalid schema")
	var scheduler = Scheduler.new()
	check(scheduler.due(9000)==0,"inactive clock")
	scheduler.start(0)
	check(scheduler.due(999)==0 and scheduler.due(1000)==1 and scheduler.due(1000)==0,"exactly once")
	check(scheduler.due(10000)==4 and scheduler.due(10000)==4 and scheduler.due(10000)==1,"debt retained")
	scheduler.stop()
	check(scheduler.due(20000)==0,"stop")
	for humans in range(1,5): run_occupancy(humans)
	determinism_and_market()
	projection_is_recursive()
	draft_disconnect_policy()
	schema_conformance()
	print("Online foundation: %d checks, %d failures" % [checks,failures])
	quit(1 if failures else 0)
func run_occupancy(humans: int) -> void:
	var app = App.new()
	for id in range(1,humans+1):
		app.connect_peer(id)
		app.request(id,message("hello",{"client":"test","client_version":"1"}),0)
		app.request(id,message("create_lobby") if id==1 else message("join_lobby",{"join_code":app.join_code}),0)
		app.request(id,message("set_ready",{"ready":true}),0)
	app.request(1,message("start_match"),0)
	check(app.phase=="draft","draft phase")
	app.outbox.clear()
	app.request(1,message("start_match"),0)
	check(app.outbox.any(func(item): return item.message.get("code","")=="invalid_phase"),"repeated start rejected")
	if humans > 1:
		var current: int = app.match_session.draft_view().turn_seat
		var wrong: int = (current+1)%humans
		app.outbox.clear()
		app.request(app.sessions.entries[wrong].connection,message("choose_foundation",{"action_id":"out-of-turn","foundation":app.match_session.draft_view().available_foundations[0]}),0)
		check(app.match_session.draft_view().turn_seat==current,"out-of-turn does not move draft")
		check(app.outbox.any(func(item): return item.message.type=="action_ack" and item.message.status!="applied"),"out-of-turn rejected")
	var safety: int = 0
	while app.phase == "draft" and safety < 4:
		var d: Dictionary = app.match_session.draft_view()
		var owner: int = d.turn_seat
		app.request(app.sessions.entries[owner].connection,message("choose_foundation",{"action_id":"draft","foundation":d.available_foundations[0]}),0)
		safety += 1
	check(app.phase=="running" and app.match_session.bots.size()==4-humans,"occupancy %d" % humans)
	for id in range(1,humans+1):
		app.request(id,message("submit_action",{"action_id":"policy","kind":"set_reduce_waste","payload":{"enabled":true}}),0)
	app.advance(1000)
	check(app.match_session.game.snapshot().tick==1,"one tick")
	var state: Dictionary = app.match_session.game.snapshot()
	check(state.cities.reduce(func(total,c):return total+c.population,0)==4000,"population")
	for seat in range(humans):
		check(state.cities[seat].reduce_waste,"human action")
		var before: String = app.match_session.game.engine.serialize()
		var v: Dictionary = View.project(state,seat)
		check(app.match_session.game.engine.serialize()==before,"view pure")
		for other in v.opponents: check(not other.has("money_micro") and not other.has("materials_micro"),"private opponents")
		var serialized: String = Protocol.Canonical.encode(v)
		for key in ["rng_state","fingerprint","command_results","last_command_seq","pending_commands","rate_carries","reserved","ledger","rejoin_token"]:
			check(not serialized.contains('"'+key+'"'),"no "+key)
	for seat in app.match_session.bots:
		check(state.cities[seat].buildings.any(func(b):return b.type=="commercial_district"),"bot construction")
	var prior_count: int = state.command_results.size()
	app.request(1,message("submit_action",{"action_id":"policy","kind":"set_reduce_waste","payload":{"enabled":true}}),1001)
	check(app.match_session.game.snapshot().command_results.size()==prior_count,"idempotent")

func determinism_and_market() -> void:
	var MatchSession = preload("res://server/match_session.gd")
	var matches: Array = []
	for reverse in [false,true]:
		var m = MatchSession.new()
		check(m.initialize("fixed-online-fixture",42,[]) == "","fixture initialization")
		while m.game.engine == null:
			var seat: int = m.game.draft.order[m.game.draft.turn]
			var card: String = m.draft_view().available_foundations[0]
			m.gateway.issue(seat,"draft","select_foundation",{"foundation":card})
		var order: Array = [3,2,1,0] if reverse else [0,1,2,3]
		for seat in order: m.gateway.issue(seat,"policy","set_reduce_waste",{"enabled":true})
		for i in range(5): check(m.step().status == "advanced","deterministic step")
		matches.append(m)
	check(matches[0].game.engine.serialize() == matches[1].game.engine.serialize(),"reversed arrival identical canonical hash")
	var m = matches[0]
	var state: Dictionary = m.game.snapshot()
	var energy: int = state.foundations.find("energy_pioneer")
	var outsider: int = (energy+1)%4
	var price: int = m.game.Sim.Balance.load_b1().market.prices.electricity.standard
	m.gateway.issue(energy,"offer","publish_offer",{"type":"electricity","rate":1000000,"price":price})
	check(m.step().status=="advanced","offer tick")
	state = m.game.snapshot()
	check(not state.offers.is_empty(),"real offer created")
	if not state.offers.is_empty():
		m.gateway.issue(outsider,"steal","withdraw_offer",{"id":state.offers[0].id})
		var result: Dictionary = m.step()
		check(result.deliveries.any(func(d): return d.message.status=="offer_not_owned"),"market ownership enforced")
	var session = preload("res://server/session_registry.gd").new()
	var token: String = session.create(2,1,"lobby",0)
	check(session.authenticate("lobby",token,1)==2,"token owner")
	check(session.authenticate("lobby",token,session.TTL_MS)==-1,"token expiry")

func projection_is_recursive() -> void:
	var MatchSession = preload("res://server/match_session.gd")
	var m = MatchSession.new()
	check(m.initialize("projection-fixture",42,[]) == "","projection fixture")
	while m.game.engine == null:
		var seat: int = m.game.draft.order[m.game.draft.turn]
		m.gateway.issue(seat,"draft-"+str(seat),"select_foundation",{"foundation":m.draft_view().available_foundations[0]})
	check(m.step().status=="advanced","projection fixture tick")
	var state: Dictionary = m.game.snapshot()
	state.event_state.active.append({"secret":"event-internal"})
	state.event_state.warnings.append({"secret":"warning-internal"})
	state.offers.append({"id":90,"seller":1,"type":{"secret":"offer-internal"},"rate":1,"price":1,"status":"open","request_id":-1,"debug":true})
	state.market.requests.append({"id":91,"buyer":0,"type":{"secret":"request-internal"},"rate":1,"status":"open","quote_until":2,"select_until":3,"debug":true})
	state.contracts.append({"id":92,"buyer":0,"seller":1,"type":{"secret":"contract-internal"},"rate":1,"price":1,"start_tick":0,"end_tick":90,"status":"active","delivered":1,"paid":1,"last_delivery":1,"booking_micro":1,"booking_refunded":false,"renewals":[0,{"secret":"renewal-internal"}],"private":true})
	state.market.notifications.append({"time":1,"contract":92,"kind":"expiry_warning","result":{"secret":"notification-internal"},"participants":[0,1]})
	state.cities[0].capacity.housing = {"secret":"city-internal"}
	for seat in range(4):
		var view: Dictionary = View.project(state,seat)
		var encoded: String = Protocol.Canonical.encode(view)
		for secret in ["event-internal","warning-internal","offer-internal","request-internal","contract-internal","renewal-internal","notification-internal","city-internal","debug","private","participants"]:
			check(not encoded.contains(secret),"nested projection hides "+secret)
		check(view.public.events.active.size() == state.event_state.active.size()-1,"public active events retained")
		check(view.marketplace.offers.any(func(o): return o.id==90),"public offers retained")
		check(view.marketplace.requests.any(func(r): return r.id==91),"public requests retained")
		check(view.marketplace.contracts.any(func(k): return k.id==92),"public contract terms retained")
		check(view.notifications.size()==(1 if seat in [0,1] else 0),"participant notification scope")
		if seat in [0,1]: check(view.own_city.contracts[0].delivered==1,"participant contract detail retained")

func create_drafting_app(now: int = 0) -> Dictionary:
	var app = App.new()
	var credentials: Array = []
	for id in [1,2]:
		app.connect_peer(id)
		app.request(id,message("hello",{"client":"test","client_version":"1"}),now)
		app.request(id,message("create_lobby") if id==1 else message("join_lobby",{"join_code":app.join_code}),now)
		credentials.append(app.outbox.filter(func(item): return item.connection==id and item.message.type=="seat_assigned")[-1].message.duplicate(true))
		app.request(id,message("set_ready",{"ready":true}),now)
	app.request(1,message("start_match"),now)
	return {"app":app,"credentials":credentials}

func draft_disconnect_policy() -> void:
	var fixture: Dictionary = create_drafting_app(100)
	var app = fixture.app
	app.draft_grace_ms = 1000
	var turn: int = app.match_session.draft_view().turn_seat
	var connection: int = app.sessions.entries[turn].connection
	var credential: Dictionary = fixture.credentials[turn]
	app.disconnect_peer(connection,100)
	check(app.phase=="draft" and app.draft_disconnect_deadlines[turn]==1100,"draft grace starts")
	app.connect_peer(3)
	app.request(3,message("hello",{"client":"test","client_version":"1"}),500)
	app.request(3,message("rejoin",{"lobby_id":credential.lobby_id,"rejoin_token":credential.rejoin_token}),500)
	check(app.sessions.seat_for(3)==turn and not app.draft_disconnect_deadlines.has(turn),"draft rejoin clears deadline")
	app.request(3,message("choose_foundation",{"action_id":"resume","foundation":app.match_session.draft_view().available_foundations[0]}),500)
	check(app.match_session.game.draft.turn>0,"draft resumes after rejoin")

	fixture = create_drafting_app(100)
	app = fixture.app
	app.draft_grace_ms = 1000
	turn = app.match_session.draft_view().turn_seat
	connection = app.sessions.entries[turn].connection
	credential = fixture.credentials[turn]
	app.disconnect_peer(connection,100)
	app.outbox.clear()
	app.advance(1100)
	check(app.phase=="waiting" and app.match_session==null and app.sessions.entries.is_empty(),"draft timeout cleans session")
	check(app.outbox.any(func(item): return item.message.type=="match_cancelled" and item.message.seat==turn),"draft timeout notification")
	app.connect_peer(4)
	app.request(4,message("hello",{"client":"test","client_version":"1"}),1200)
	app.request(4,message("rejoin",{"lobby_id":credential.lobby_id,"rejoin_token":credential.rejoin_token}),1200)
	check(app.outbox.any(func(item): return item.connection==4 and item.message.get("code","")=="authentication_failed"),"cancelled credential rejected")
	var survivor: int = 1 if connection != 1 else 2
	app.request(survivor,message("choose_foundation",{"action_id":"stale","foundation":"industrial_legacy"}),1200)
	check(app.outbox.any(func(item): return item.connection==survivor and item.message.get("code","")=="not_seated"),"stale draft command rejected")

	check_draft_rejoin_boundary(1099,true,"deadline minus one")
	check_draft_rejoin_boundary(1100,false,"exact deadline")
	check_draft_rejoin_boundary(1101,false,"deadline plus one")

	fixture = create_drafting_app(100)
	app = fixture.app
	app.draft_grace_ms = 1000
	turn = app.match_session.draft_view().turn_seat
	var other: int = 1 if turn == 0 else 0
	app.disconnect_peer(app.sessions.entries[other].connection,100)
	app.outbox.clear()
	var turn_connection: int = app.sessions.entries[turn].connection
	var available: String = app.match_session.draft_view().available_foundations[0]
	app.request(turn_connection,message("choose_foundation",{"action_id":"blocked-by-other-timeout","foundation":available}),1100)
	check(app.phase=="waiting" and app.match_session==null,"expired other participant cancels before request")
	check(not app.outbox.any(func(item): return item.connection==turn_connection and item.message.get("status","")=="applied"),"request cannot progress past another deadline")

func check_draft_rejoin_boundary(now: int, succeeds: bool, label: String) -> void:
	var fixture: Dictionary = create_drafting_app(100)
	var app = fixture.app
	app.draft_grace_ms = 1000
	var turn: int = app.match_session.draft_view().turn_seat
	var old_connection: int = app.sessions.entries[turn].connection
	var credential: Dictionary = fixture.credentials[turn]
	app.disconnect_peer(old_connection,100)
	app.outbox.clear()
	app.connect_peer(3)
	app.request(3,message("hello",{"client":"test","client_version":"1"}),now)
	app.request(3,message("rejoin",{"lobby_id":credential.lobby_id,"rejoin_token":credential.rejoin_token}),now)
	if succeeds:
		check(app.phase=="draft" and app.sessions.seat_for(3)==turn,label+" rejoin succeeds")
		check(not app.draft_disconnect_deadlines.has(turn),label+" clears deadline")
	else:
		check(app.phase=="waiting" and app.match_session==null,label+" cancels draft")
		check(app.outbox.any(func(item): return item.connection==3 and item.message.get("code","")=="authentication_failed"),label+" rejects rejoin")
		if now == 1100:
			var survivor: int = 1 if old_connection != 1 else 2
			app.request(survivor,message("create_lobby"),now)
			check(app.lobby_id!="" and app.sessions.seat_for(survivor)==0,label+" permits fresh lobby")

func schema_conformance() -> void:
	var parsed: Variant = JSON.parse_string(FileAccess.get_file_as_string("res://shared/protocol_v1.schema.json"))
	check(typeof(parsed)==TYPE_DICTIONARY,"schema parses")
	if typeof(parsed)!=TYPE_DICTIONARY: return
	var client_types: Array = []
	var action_kinds: Array = []
	for definition in parsed["$defs"].clientMessage.oneOf:
		check(definition.get("additionalProperties",true)==false,"client schema closes fields")
		var kind: String = definition.properties.type.const
		if kind not in client_types: client_types.append(kind)
		if kind == "submit_action":
			var action: String = definition.properties.kind.const
			action_kinds.append(action)
			var required: Array = definition.properties.payload.required.duplicate()
			required.sort()
			var expected: Array = Protocol.PAYLOADS[action].keys()
			expected.sort()
			check(required==expected,"schema payload fields "+action)
			check(definition.properties.payload.additionalProperties==false,"schema payload closed "+action)
	client_types.sort()
	var implemented_types: Array = Protocol.FIELDS.keys()
	implemented_types.sort()
	action_kinds.sort()
	var implemented_actions: Array = Protocol.PAYLOADS.keys()
	implemented_actions.sort()
	check(client_types==implemented_types,"schema client message types")
	check(action_kinds==implemented_actions,"schema action kinds")
	var server_types: Array = []
	for definition in parsed["$defs"].serverMessage.oneOf:
		server_types.append(definition.properties.type.const)
		check(definition.get("additionalProperties",true)==false,"server schema closes fields")
	server_types.sort()
	var expected_server: Array = ["welcome","protocol_error","seat_assigned","lobby_created","lobby_state","draft_state","match_started","action_ack","player_view","match_cancelled","match_finished","catalogue","action_preview"]
	expected_server.sort()
	check(server_types==expected_server,"schema server message types")
