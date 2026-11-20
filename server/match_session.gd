extends RefCounted
const Life = preload("res://simulation/interfaces/lifecycle.gd")
const Gateway = preload("res://server/command_gateway.gd")
const Bots = preload("res://server/bot_controller.gd")
const Scheduler = preload("res://server/tick_scheduler.gd")
const View = preload("res://simulation/projections/player_view.gd")
const Protocol = preload("res://shared/protocol_v1.gd")
const Presentation = preload("res://server/presentation.gd")
var presentation = Presentation.new()
var game = Life.new()
var gateway = Gateway.new(game)
var scheduler = Scheduler.new()
var bots: Array = []
var view_seq: int = 0
var failed: bool = false
func initialize(match_id: String, seed_value: int, bot_seats: Array) -> String:
	bots = bot_seats.duplicate()
	var error: String = game.create(match_id,seed_value)
	if error == "": Bots.draft(game,gateway,bots)
	return error
func draft_view() -> Dictionary:
	var available: Array = []
	for card in game.CARDS:
		if card not in game.draft.choices: available.append(card)
	return Protocol.message("draft_state",{"match_id":game.draft.match_id,"order":game.draft.order.duplicate(),"choices":game.draft.choices.duplicate(),"turn_seat":game.draft.order[game.draft.turn] if game.engine == null else -1,"available_foundations":available})
func view(seat: int) -> Dictionary:
	var state: Dictionary = game.snapshot()
	var result: Dictionary = View.project(state,seat)
	result.presentation = presentation.details(game.engine,state,seat)
	result.merge({"match_id":state.match_id,"tick":state.tick,"view_seq":view_seq})
	return Protocol.message("player_view",result)
func step() -> Dictionary:
	if failed: return {"status":"session_failed"}
	if game.engine == null: return {"status":"draft_in_progress"}
	if game.snapshot().status == "finished": return {"status":"match_closed"}
	Bots.play(game,gateway,bots)
	var result: Dictionary = game.step()
	if result.status != "advanced":
		failed = true
		scheduler.stop()
		return result
	result.deliveries = gateway.complete(result)
	view_seq += 1
	if game.snapshot().status == "finished": scheduler.stop()
	return result
