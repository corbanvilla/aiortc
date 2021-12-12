import asyncio
import json
import logging
import os
import random
import sys

from aiortc import RTCIceCandidate, RTCSessionDescription
from aiortc.sdp import candidate_from_sdp, candidate_to_sdp

try:
    import aiohttp
    import websockets
except ImportError:  # pragma: no cover
    aiohttp = None
    websockets = None

logger = logging.getLogger(__name__)
BYE = object()


def object_from_string(message_str):
    message = json.loads(message_str)
    if message["type"] in ["answer", "offer"]:
        return RTCSessionDescription(**message)
    elif message["type"] == "candidate" and message.get("candidate"):
        candidate = candidate_from_sdp(message["candidate"].split(":", 1)[1])
        candidate.sdpMid = message["id"]
        candidate.sdpMLineIndex = message["label"]
        return candidate
    elif message["type"] == "bye":
        return BYE


def object_to_string(obj):
    if isinstance(obj, RTCSessionDescription):
        message = {"sdp": obj.sdp, "type": obj.type}
    elif isinstance(obj, RTCIceCandidate):
        message = {
            "candidate": "candidate:" + candidate_to_sdp(obj),
            "id": obj.sdpMid,
            "label": obj.sdpMLineIndex,
            "type": "candidate",
        }
    else:
        assert obj is BYE
        message = {"type": "bye"}
    return json.dumps(message, sort_keys=True)


class ApprtcSignaling:
    def __init__(self, room):
        self._http = None
        self._origin = "https://appr.tc"
        self._room = room
        self._websocket = None

    async def connect(self):
        join_url = self._origin + "/join/" + self._room

        # fetch room parameters
        self._http = aiohttp.ClientSession()
        async with self._http.post(join_url) as response:
            # we cannot use response.json() due to:
            # https://github.com/webrtc/apprtc/issues/562
            data = json.loads(await response.text())
        assert data["result"] == "SUCCESS"
        params = data["params"]

        self.__is_initiator = params["is_initiator"] == "true"
        self.__messages = params["messages"]
        self.__post_url = (
            self._origin + "/message/" + self._room + "/" + params["client_id"]
        )

        # connect to websocket
        self._websocket = await websockets.connect(
            params["wss_url"], extra_headers={"Origin": self._origin}
        )
        await self._websocket.send(
            json.dumps(
                {
                    "clientid": params["client_id"],
                    "cmd": "register",
                    "roomid": params["room_id"],
                }
            )
        )

        print(f"AppRTC room is {params['room_id']} {params['room_link']}")

        return params

    async def close(self):
        if self._websocket:
            await self.send(BYE)
            await self._websocket.close()
        if self._http:
            await self._http.close()

    async def receive(self):
        if self.__messages:
            message = self.__messages.pop(0)
        else:
            message = await self._websocket.recv()
            message = json.loads(message)["msg"]
        logger.info("< " + message)
        return object_from_string(message)

    async def send(self, obj):
        message = object_to_string(obj)
        logger.info("> " + message)
        if self.__is_initiator:
            await self._http.post(self.__post_url, data=message)
        else:
            await self._websocket.send(json.dumps({"cmd": "send", "msg": message}))


class CopyAndPasteSignaling:
    def __init__(self):
        self._read_pipe = sys.stdin
        self._read_transport = None
        self._reader = None
        self._write_pipe = sys.stdout

    async def connect(self):
        loop = asyncio.get_event_loop()
        self._reader = asyncio.StreamReader(loop=loop)
        self._read_transport, _ = await loop.connect_read_pipe(
            lambda: asyncio.StreamReaderProtocol(self._reader), self._read_pipe
        )

    async def close(self):
        if self._reader is not None:
            await self.send(BYE)
            self._read_transport.close()
            self._reader = None

    async def receive(self):
        print("-- Please enter a message from remote party --")
        data = await self._reader.readline()
        print()
        return object_from_string(data.decode(self._read_pipe.encoding))

    async def send(self, descr):
        print("-- Please send this message to the remote party --")
        self._write_pipe.write(object_to_string(descr) + "\n")
        self._write_pipe.flush()
        print()


class TcpSocketSignaling:
    def __init__(self, host, port):
        self._host = host
        self._port = port
        self._server = None
        self._reader = None
        self._writer = None

    async def connect(self):
        pass

    async def _connect(self, server):
        if self._writer is not None:
            return

        if server:
            connected = asyncio.Event()

            def client_connected(reader, writer):
                self._reader = reader
                self._writer = writer
                connected.set()

            self._server = await asyncio.start_server(
                client_connected, host=self._host, port=self._port
            )
            await connected.wait()
        else:
            self._reader, self._writer = await asyncio.open_connection(
                host=self._host, port=self._port
            )

    async def close(self):
        if self._writer is not None:
            await self.send(BYE)
            self._writer.close()
            self._reader = None
            self._writer = None
        if self._server is not None:
            self._server.close()
            self._server = None

    async def receive(self):
        await self._connect(False)
        try:
            data = await self._reader.readuntil()
        except asyncio.IncompleteReadError:
            return
        return object_from_string(data.decode("utf8"))

    async def send(self, descr):
        await self._connect(True)
        data = object_to_string(descr).encode("utf8")
        self._writer.write(data + b"\n")


class UnixSocketSignaling:
    def __init__(self, path):
        self._path = path
        self._server = None
        self._reader = None
        self._writer = None

    async def connect(self):
        pass

    async def _connect(self, server):
        if self._writer is not None:
            return

        if server:
            connected = asyncio.Event()

            def client_connected(reader, writer):
                self._reader = reader
                self._writer = writer
                connected.set()

            self._server = await asyncio.start_unix_server(
                client_connected, path=self._path
            )
            await connected.wait()
        else:
            self._reader, self._writer = await asyncio.open_unix_connection(self._path)

    async def close(self):
        if self._writer is not None:
            await self.send(BYE)
            self._writer.close()
            self._reader = None
            self._writer = None
        if self._server is not None:
            self._server.close()
            self._server = None
            os.unlink(self._path)

    async def receive(self):
        await self._connect(False)
        try:
            data = await self._reader.readuntil()
        except asyncio.IncompleteReadError:
            return
        return object_from_string(data.decode("utf8"))

    async def send(self, descr):
        await self._connect(True)
        data = object_to_string(descr).encode("utf8")
        self._writer.write(data + b"\n")


class NodeDSSSignaling:
    def __init__(self, signal_server, local_peer, remote_peer):
        self._http = None
        self._signal_server = signal_server
        self.local_peer = local_peer
        self.remote_peer = remote_peer

        self.__post_url = self._signal_server + "/data/" + self.remote_peer
        self.__get_url = self._signal_server + "/data/" + self.local_peer

    @staticmethod
    def _standardize_message_str(message_str: str) -> str:
        """
        Microsoft has their own separate spec for the keys
        sent in the SDP negotiation:

        'type' is known as the enum 'MessageType':
            - 0: Unknown message
            - 1: Offer message
            - 2: Answer message
            - 3: Candidate Message
        'sdp' is known as 'Data'
        """
        message_type_mapping = {
            0: 'unknown',
            1: 'offer',
            2: 'answer',
            3: 'candidate'
        }
        message = json.loads(message_str)

        # Update type field
        message['type'] = message_type_mapping[message.pop('MessageType')]

        # Drop + store IceDataSeparate field if exists
        ice_separator = message.pop('IceDataSeparator', None)

        # Offer/answer formats
        if message['type'] in ['answer', 'offer']:
            # Update sdp field
            message['sdp'] = message.pop('Data')

        # Candidate formats
        elif message['type'] == 'candidate' and ice_separator:
            # Converts:
            # label -> SdpMlineIndex
            # id -> SdpMid
            message['candidate'], message['label'], message['id'] = message.pop('Data').split(ice_separator)

        return json.dumps(message)

    @staticmethod
    def _create_compliant_message(message_str: str) -> str:
        """
        Reverse operation of `_standardize_response_str`
        """
        message_type_mapping = {
            'unknown': 0,
            'offer': 1,
            'answer': 2,
            'candidate': 3
        }
        message = json.loads(message_str)

        type = message.pop("type")
        # Update type to 'MessageType' enum
        message['MessageType'] = message_type_mapping[type]

        if type in ["answer", "offer"]:
            # Update sdp field to 'Data'
            message['Data'] = message.pop('sdp')
            # Ice separator
            message['IceDataSeparator'] = ""

        elif type == 'candidate':
            ice_separator = "|"  # this  is hard-coded into Microsoft's code.
            message['IceDataSeparator'] = ice_separator
            message['Data'] = f'{message.pop("candidate")}{ice_separator}' \
                              f'{message.pop("label")}{ice_separator}' \
                              f'{message.pop("id")}'

        return json.dumps(message)

    async def connect(self):
        self._http = aiohttp.ClientSession()

    async def close(self):
        if self._http:
            await self._http.close()

    async def receive(self):
        async with self._http.get(self.__get_url) as response:
            # we cannot use response.json() due to:
            # https://github.com/webrtc/apprtc/issues/562
            message = await response.text()
            if not message:
                return
            # Make compliant
            message = self._standardize_message_str(message)
        logger.info("< " + str(message))
        return object_from_string(message)

    async def send(self, obj):
        message = object_to_string(obj)
        # Make compliant
        message = self._create_compliant_message(message)
        logger.info("> " + message)
        await self._http.post(self.__post_url, data=message)


def add_signaling_arguments(parser):
    """
    Add signaling method arguments to an argparse.ArgumentParser.
    """
    parser.add_argument(
        "--signaling",
        "-s",
        choices=["apprtc", "copy-and-paste", "tcp-socket", "unix-socket"],
    )
    parser.add_argument(
        "--signaling-host", default="127.0.0.1", help="Signaling host (tcp-socket only)"
    )
    parser.add_argument(
        "--signaling-port", default=1234, help="Signaling port (tcp-socket only)"
    )
    parser.add_argument(
        "--signaling-path",
        default="aiortc.socket",
        help="Signaling socket path (unix-socket only)",
    )
    parser.add_argument(
        "--signaling-room", default=None, help="Signaling room (apprtc only)"
    )


def create_signaling(args):
    """
    Create a signaling method based on command-line arguments.
    """
    if args.signaling == "apprtc":
        if aiohttp is None or websockets is None:  # pragma: no cover
            raise Exception("Please install aiohttp and websockets to use appr.tc")
        if not args.signaling_room:
            args.signaling_room = "".join(
                [random.choice("0123456789") for x in range(10)]
            )
        return ApprtcSignaling(args.signaling_room)
    elif args.signaling == "tcp-socket":
        return TcpSocketSignaling(args.signaling_host, args.signaling_port)
    elif args.signaling == "unix-socket":
        return UnixSocketSignaling(args.signaling_path)
    else:
        return CopyAndPasteSignaling()
