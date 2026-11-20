extends RefCounted
const Protocol = preload("res://shared/protocol_v1.gd")
var tcp = TCPServer.new()
var peers: Dictionary = {}
var next_id: int = 0
var app: RefCounted
var closing: bool = false
func _init(application: RefCounted) -> void:
	app = application
func listen(address: String, port: int) -> Error:
	return tcp.listen(port,address)
func drop(id: int, code: String = "") -> void:
	if not peers.has(id): return
	var peer: Dictionary = peers[id]
	if peer.closing: return
	if code != "": peer.socket.send_text(Protocol.Canonical.encode(Protocol.message("protocol_error",{"code":code,"fatal":true})))
	peer.socket.close(1008,code)
	peer.closing = true
	peer.closed_at = Time.get_ticks_msec()
	app.disconnect_peer(id)
func poll(now: int) -> void:
	while tcp.is_connection_available():
		var stream: StreamPeerTCP = tcp.take_connection()
		if peers.size() >= 16 or closing:
			stream.disconnect_from_host()
			continue
		var socket = WebSocketPeer.new()
		socket.inbound_buffer_size = 65536
		socket.outbound_buffer_size = 1048576
		socket.max_queued_packets = 128
		socket.heartbeat_interval = 10.0
		if socket.accept_stream(stream) != OK:
			stream.disconnect_from_host()
			continue
		next_id += 1
		peers[next_id] = {"socket":socket,"stream":stream,"born":now,"window":now,"count":0,"closing":false,"closed_at":0,"last_rx":now}
		app.connect_peer(next_id)
	for id in peers.keys():
		var p: Dictionary = peers[id]
		p.socket.poll()
		var state: int = p.socket.get_ready_state()
		if state == WebSocketPeer.STATE_CLOSED or (p.closing and now-p.closed_at > 1000):
			app.disconnect_peer(id)
			p.stream.disconnect_from_host()
			peers.erase(id)
			continue
		if p.closing: continue
		if not app.connections.get(id,false) and now-p.born > 10000:
			drop(id,"connection_timeout")
			continue
		if state != WebSocketPeer.STATE_OPEN: continue
		if now-p.window >= 1000: p.window = now; p.count = 0
		while p.socket.get_available_packet_count() > 0:
			var data: PackedByteArray = p.socket.get_packet()
			p.last_rx = now
			p.count += 1
			if p.count > 20: drop(id,"rate_limited"); break
			if not p.socket.was_string_packet(): drop(id,"binary_frame"); break
			var decoded: Dictionary = Protocol.decode(data)
			if decoded.error != "":
				var context: Dictionary = {}
				if typeof(decoded.get("value")) == TYPE_DICTIONARY and typeof(decoded.value.get("correlation_id")) == TYPE_STRING and Protocol.identifier(decoded.value.correlation_id):
					context.correlation_id = decoded.value.correlation_id
				app.reject(id,context,decoded.error)
			else: app.request(id,decoded.value,now)
	flush()
func flush() -> void:
	for id in app.disconnect_ids: drop(id,"session_replaced")
	app.disconnect_ids.clear()
	# Appending disconnect notifications while flushing is deferred to next poll.
	var outgoing: Array = app.outbox
	app.outbox = []
	for item in outgoing:
		if not peers.has(item.connection): continue
		var p: Dictionary = peers[item.connection]
		if p.closing: continue
		if p.socket.get_current_outbound_buffered_amount() > 524288:
			drop(item.connection,"slow_client")
			continue
		if p.socket.send_text(Protocol.Canonical.encode(item.message)) != OK: drop(item.connection,"send_failed")
func shutdown() -> void:
	closing = true
	tcp.stop()
	for id in peers: drop(id,"server_shutdown")
