"""Standalone mock server for OPC-UA binary (ports 4840, 49320, 62541) and Rockwell CIP (port 44818)."""
import asyncio
import logging
import struct
import sys

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("SimMockServer")

# OPC-UA Binary HEL/ACK Handshake
# HEL message: MessageType 'HEL' (3 bytes), ChunkType 'F' (1 byte), MessageSize (uint32)
# ACK message: 'ACK' + 'F' + size (28) + version(0) + recvBuf(65535) + sendBuf(65535) + maxMsg(1048576) + maxChunks(100)
OPC_ACK_PACKET = struct.pack("<4sIIIIII", b"ACKF", 28, 0, 65535, 65535, 1048576, 100)


async def handle_opc_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, port: int):
    peer = writer.get_extra_info("peername")
    logger.info("OPC-UA connection from %s on port %d", peer, port)
    try:
        while not reader.at_eof():
            try:
                header = await reader.readexactly(8)
            except (asyncio.IncompleteReadError, ConnectionResetError):
                break
            msg_type = header[:4]
            msg_size = struct.unpack("<I", header[4:8])[0]
            # Read remainder of the message
            if msg_size > 8:
                try:
                    remaining = await reader.readexactly(msg_size - 8)
                except (asyncio.IncompleteReadError, ConnectionResetError):
                    break
            if msg_type == b"HELF":
                logger.info("Received OPC-UA HEL packet from %s on port %d, replying with ACK", peer, port)
                writer.write(OPC_ACK_PACKET)
                await writer.drain()
            else:
                # Echo dummy response or acknowledge close
                if msg_type == b"CLOF":
                    break
    except Exception as exc:
        logger.debug("OPC connection error on port %d: %s", port, exc)
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass


# EtherNet/IP CIP Encapsulation Handshake
# Command 0x0065 = RegisterSession
async def handle_cip_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    peer = writer.get_extra_info("peername")
    logger.info("Rockwell CIP connection from %s on port 44818", peer)
    try:
        while not reader.at_eof():
            try:
                header = await reader.readexactly(24)
            except (asyncio.IncompleteReadError, ConnectionResetError):
                break
            cmd, length, session, status, context, options = struct.unpack("<HHII8sI", header)
            data = b""
            if length > 0:
                try:
                    data = await reader.readexactly(length)
                except (asyncio.IncompleteReadError, ConnectionResetError):
                    break
            if cmd == 0x0065:
                # RegisterSession reply: cmd=0x0065, length=4, session=1, status=0, context, options=0, protocolVersion=1, options=0
                reply_header = struct.pack("<HHII8sI", 0x0065, 4, 1, 0, context, 0)
                reply_body = struct.pack("<HH", 1, 0)
                writer.write(reply_header + reply_body)
                await writer.drain()
            else:
                # Standard success reply for generic encapsulation
                reply_header = struct.pack("<HHII8sI", cmd, 0, session if session else 1, 0, context, 0)
                writer.write(reply_header)
                await writer.drain()
    except Exception as exc:
        logger.debug("CIP error: %s", exc)
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass


async def main():
    servers = []
    # OPC-UA listeners
    for port in [4840, 49320, 62541]:
        srv = await asyncio.start_server(
            lambda r, w, p=port: handle_opc_client(r, w, p),
            "0.0.0.0",
            port,
        )
        servers.append(srv)
        logger.info("Mock OPC-UA server listening on 0.0.0.0:%d", port)

    # Rockwell CIP listener
    cip_srv = await asyncio.start_server(handle_cip_client, "0.0.0.0", 44818)
    servers.append(cip_srv)
    logger.info("Mock Rockwell CIP / Logix server listening on 0.0.0.0:44818")

    async with asyncio.TaskGroup() as tg:
        for s in servers:
            tg.create_task(s.serve_forever())


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Shutting down mock server.")
