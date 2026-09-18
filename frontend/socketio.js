/**
 * A minimal Socket.IO client — just enough of the protocol to subscribe to a case and
 * receive what the server pushes back. No CDN, no build step, no reconnection logic.
 *
 * Two layers are in play, and both put their type in the first character(s):
 *
 *   Engine.IO   0 open   1 close   2 ping   3 pong   4 message
 *   Socket.IO   (inside a "4")  0 connect  1 disconnect  2 event
 *
 * So `42["case",{...}]` reads as: Engine.IO message, Socket.IO event, named "case".
 */

export function connect(baseUrl, { onOpen, onClose } = {}) {
  const url = new URL('/socket.io/', baseUrl);
  url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:';
  url.searchParams.set('EIO', '4');
  url.searchParams.set('transport', 'websocket');

  const socket = new WebSocket(url);
  const handlers = new Map();
  const queued = [];
  let ready = false;

  const send = (frame) => socket.readyState === WebSocket.OPEN && socket.send(frame);

  socket.addEventListener('message', ({ data }) => {
    if (data[0] === '2') return send('3');          // ping -> pong, or we get dropped
    if (data[0] === '0') return send('40');         // engine open -> join the namespace
    if (data.slice(0, 2) === '40') {                // namespace joined
      ready = true;
      queued.splice(0).forEach(send);
      onOpen?.();
      return;
    }
    if (data.slice(0, 2) !== '42') return;

    // An event may carry an ack id between the type and the payload: 42[...] or 4217[...]
    const [name, payload] = JSON.parse(data.slice(2).replace(/^\d+/, ''));
    handlers.get(name)?.forEach((fn) => fn(payload));
  });

  socket.addEventListener('close', () => onClose?.());

  return {
    on(name, fn) {
      handlers.set(name, [...(handlers.get(name) ?? []), fn]);
      return this;
    },
    /** Send an event. Queued until the namespace handshake finishes. */
    emit(name, payload) {
      const frame = '42' + JSON.stringify([name, payload]);
      ready ? send(frame) : queued.push(frame);
      return this;
    },
    close() {
      send('41');
      socket.close();
    },
  };
}
