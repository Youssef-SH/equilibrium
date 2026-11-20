extends SceneTree
const App = preload("res://server/application.gd")
const Protocol = preload("res://shared/protocol_v1.gd")
const Canonical = preload("res://simulation/core/canonical.gd")
var checks: int = 0
var failures: int = 0
func check(ok: bool, label: String) -> void:
	checks += 1
	if not ok:
		failures += 1
		printerr("FAIL: "+label)
func request(app: RefCounted, id: int, kind: String, body: Dictionary = {}) -> Dictionary:
	var m: Dictionary = Protocol.message(kind,body)
	m.correlation_id = "query"
	app.request(id,m,0)
	return app.outbox[-1].message
func _initialize() -> void:
	var app = App.new()
	app.connect_peer(1)
	check(request(app,1,"get_catalogue").code=="hello_required","catalogue needs negotiation")
	request(app,1,"hello",{"client":"test","client_version":"1"})
	check(request(app,1,"get_catalogue").code=="not_seated","catalogue needs seat")
	check(request(app,1,"preview_action",{"kind":"construct","payload":{"building_type":"factory"}}).code=="not_seated","preview needs seat")
	request(app,1,"create_lobby")
	var catalogue: Dictionary = request(app,1,"get_catalogue")
	check(catalogue.type=="catalogue" and catalogue.buildings.size()==11,"all public buildings")
	check(catalogue.foundations.size()==4 and catalogue.foundations.investment_district.money==900,"actual Foundation packages")
	check(not catalogue.has("initial_waste") and not catalogue.has("rng_state"),"no unrestricted config")
	check(request(app,1,"preview_action",{"kind":"construct","payload":{"building_type":"factory"}}).code=="invalid_phase","no preview before play")
	request(app,1,"set_ready",{"ready":true})
	request(app,1,"start_match")
	var available: Array = app.match_session.draft_view().available_foundations
	request(app,1,"choose_foundation",{"action_id":"draft","foundation":available[0]})
	check(app.phase=="running","fixture runs")
	var engine = app.match_session.game.engine
	var before: String = engine.serialize()
	var seq: Array = app.match_session.gateway.sequences.duplicate()
	var preview: Dictionary = request(app,1,"preview_action",{"kind":"construct","payload":{"building_type":"residential_district","tile":10}})
	check(preview.status=="available","legal preview")
	check(preview.purchase.base_cost_micro==150000000 and preview.purchase.cash_micro==150000000,"authoritative cost")
	check(preview.purchase.materials_micro==0 and preview.purchase.duration_seconds==22,"materials and duration")
	check(engine.serialize()==before and app.match_session.gateway.sequences==seq,"preview preserves state and command sequences")
	for kind in Protocol.PAYLOADS:
		var payload: Dictionary = {}
		for key in Protocol.PAYLOADS[kind]:
			payload[key] = 0 if Protocol.PAYLOADS[kind][key]==TYPE_INT else false if Protocol.PAYLOADS[kind][key]==TYPE_BOOL else "invalid"
		var result: Dictionary = request(app,1,"preview_action",{"kind":kind,"payload":payload})
		check(result.type=="action_preview","all gameplay previews: "+kind)
	check(engine.serialize()==before and app.match_session.gateway.sequences==seq,"invalid queries also pure")
	var stolen: Dictionary = request(app,1,"preview_action",{"kind":"construct","payload":{"building_type":"factory"},"seat":2})
	check(stolen.code=="invalid_fields","cannot select another seat")
	var plant_id: int = engine.snapshot().cities[1].buildings[0].id if not engine.snapshot().cities[1].buildings.is_empty() else -1
	if plant_id>=0:
		check(request(app,1,"preview_action",{"kind":"disable","payload":{"building_id":plant_id}}).status=="building_not_owned","other building cannot be controlled")
	for _i in range(3):
		check(app.match_session.step().status=="advanced","short fixture tick")
	var state_before: String = engine.serialize()
	var circular: int = engine.snapshot().foundations.find("circular_foundation")
	var material_preview: Dictionary = app.presentation.preview(engine,circular,"construct",{"building_type":"residential_district","tile":15})
	check(material_preview.purchase.materials_micro>0,"real recovered materials discount")
	check(material_preview.purchase.cash_micro+material_preview.purchase.materials_micro==material_preview.purchase.base_cost_micro,"discount reconciles")
	check(engine.serialize()==state_before,"discount preview preserves recovered inventory")
	var view: Dictionary = app.match_session.view(0)
	check(engine.serialize()==state_before,"presentation is read-only")
	check(view.presentation.buildings.size()==view.own_city.buildings.size(),"own building details")
	check(view.presentation.next_migration==80,"migration timing")
	for opponent in view.opponents:
		check(not opponent.has("money_micro") and not opponent.has("buildings"),"opponent privacy")
	for b in view.presentation.buildings:
		check(not b.has("owner_city") and not b.effects.has("secret"),"selected detail fields")
	# Terminal-boundary fixture only: preceding 1,199 ticks are deliberately
	# omitted. Exercise actual finalization without claiming a complete match.
	var terminal = preload("res://server/match_session.gd").new()
	check(terminal.initialize("terminal-fixture",42,[])=="","terminal fixture initialization")
	for _i in range(4):
		var draft: Dictionary = terminal.draft_view()
		terminal.gateway.issue(draft.turn_seat,"final-draft","select_foundation",{"foundation":draft.available_foundations[0]})
	terminal.game.engine._state.tick = 1199
	check(terminal.step().status=="advanced","one actual terminal-boundary transition")
	var final_view: Dictionary = terminal.view(0)
	check(final_view.final_result != null and final_view.tick==1200,"authoritative final results fixture")
	var args: PackedStringArray = OS.get_cmdline_user_args()
	if args.size()==1:
		var output = FileAccess.open(args[0],FileAccess.WRITE)
		output.store_string(Canonical.encode({"view":view,"views":[app.match_session.view(0),app.match_session.view(1),app.match_session.view(2),app.match_session.view(3)],"catalogue":catalogue,"preview":preview,"final_view":final_view}))
	print("Presentation: %d checks, %d failures" % [checks,failures])
	quit(1 if failures else 0)
