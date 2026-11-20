extends RefCounted
## Selected public definitions and authenticated, non-mutating engine queries.
const Balance = preload("res://simulation/core/balance.gd")
const View = preload("res://simulation/projections/player_view.gd")
const Insights = preload("res://simulation/projections/insights.gd")
const A = preload("res://simulation/core/arithmetic.gd")
var balance: Dictionary = Balance.load_b1()

func catalogue() -> Dictionary:
	var result: Dictionary = {"buildings":{},"foundations":{},"prices":{},"events":[]}
	for kind in Balance.TYPES:
		var b: Dictionary = balance.buildings[kind]
		result.buildings[kind] = View.pick(b,["build_unlock_micro","upgrade_unlock_micro"])
		for tier in ["l1","upgrade_delta"]:
			result.buildings[kind][tier] = View.pick(b[tier],Balance.EFFECTS)
	for key in balance.foundations:
		result.foundations[key] = View.pick(balance.foundations[key],["money","building"])
	for key in balance.market.prices:
		result.prices[key] = View.pick(balance.market.prices[key],["minimum","maximum","step","standard"])
	result.rules = View.pick(balance,["micro_units","match_seconds","grid_width","grid_height","construction_slots","migration_first_second","migration_interval_seconds"])
	result.policy = View.pick(balance.economy,["policy_fee_micro_per_minute","policy_cooldown_seconds","policy_factory_income_percent","policy_commerce_income_percent","cancel_refund_percent","demolition_refund_micro"])
	result.policy.merge(View.pick(balance.local_simulation,["policy_household_percent","policy_factory_percent","policy_commerce_percent"]))
	result.market = View.pick(balance.market,["duration","minimum_remaining","booking_micro","quote_seconds","selection_seconds","expiry_warning"])
	for event in balance.events:
		var item: Dictionary = View.pick(event,["id","warning","start","end"])
		item.modifiers = View.pick(event.modifiers,["air_percent","household_percent","factory_percent","commerce_percent","municipal_E","uncollected_penalty_micro"])
		result.events.append(item)
	return result

func preview(engine: RefCounted, seat: int, kind: String, payload: Dictionary) -> Dictionary:
	var error: String = engine.preview(seat,kind,payload)
	var state: Dictionary = engine.snapshot()
	var result: Dictionary = {"status":"available" if error=="" else error,"tick":state.tick,"kind":kind,"payload":payload.duplicate(true)}
	var definition: Dictionary = {}
	var building_type: String = ""
	if kind == "construct" and balance.buildings.has(payload.get("building_type","")):
		building_type = payload.building_type
		definition = balance.buildings[building_type].l1
	elif kind == "upgrade":
		var plant: Dictionary = engine._buildings.find_building(state.cities[seat],payload.get("building_id",-1))
		if not plant.is_empty():
			building_type = plant.type
			definition = balance.buildings[plant.type].upgrade_delta
	if not definition.is_empty():
		var payment: Dictionary = engine._buildings.quote(state.cities[seat],definition.cost_micro)
		result.purchase = {"base_cost_micro":definition.cost_micro,"cash_micro":payment.cash,"materials_micro":payment.materials,"duration_seconds":definition.duration_seconds,"unlock_micro":balance.buildings[building_type]["build_unlock_micro" if kind=="construct" else "upgrade_unlock_micro"]}
		result.effects = View.pick(definition,Balance.EFFECTS)
	return result

func details(engine: RefCounted, state: Dictionary, seat: int) -> Dictionary:
	var city: Dictionary = state.cities[seat]
	var info: Dictionary = Insights.city(state,seat,balance,engine._buildings)
	var result: Dictionary = {"buildings":[],"conditions":[],"last_migration_delta":info.last_migration_delta,"next_migration":null,"collection_micro_per_minute":balance.collection_units_per_minute*A.S,"landfill_micro_per_minute":balance.landfill_units_per_minute*A.S}
	for condition in info.conditions:
		# Do not expose insight measurements containing other cities' hidden state.
		result.conditions.append(View.pick(condition,["condition","severity","cause","effect","relation"]))
	for tick in range(balance.migration_first_second,balance.match_seconds+1,balance.migration_interval_seconds):
		if tick>state.tick:
			result.next_migration=tick
			break
	for plant in city.buildings:
		var effects: Dictionary = engine._buildings.effects(plant)
		var efficiency: int = A.efficiency(city,plant)
		result.buildings.append({"id":plant.id,"effects":View.pick(effects,Balance.EFFECTS),"efficiency_micro":efficiency})
		result.collection_micro_per_minute += A.mul(effects.collection_per_minute*A.S,efficiency)
		if plant.type=="landfill": result.landfill_micro_per_minute += A.mul(effects.treatment_per_minute*A.S,efficiency)
	return result
