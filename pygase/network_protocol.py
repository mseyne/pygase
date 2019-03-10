# -*- coding: utf-8 -*-

import time

import curio

from pygase.utils import Sendable, NamedEnum, sqn

class ProtocolIDMismatchError(ValueError):
    pass

class DuplicateSequenceError(ConnectionError):
    pass

class Package:

    timeout = 1 # package timeout in seconds
    max_size = 2048 # the maximum size of Pygase package in bytes
    _protocol_id = bytes.fromhex('ffd0fab9') # unique 4 byte identifier for pygase packages

    def __init__(self, sequence: int, ack: int, ack_bitfield: str, payload:bytes=None):
        self.sequence = sqn(sequence)
        self.ack = sqn(ack)
        self.ack_bitfield = ack_bitfield
        self.payload = payload # event protocol (contains various events + stateupdate if for client or last known state if for server)

    def to_datagram(self):
        '''
        ### Returns
          *bytes*: compact bytestring representing the package, which can be sent via a datagram socket
        '''
        datagram = bytearray(self._protocol_id)
        datagram.extend(self.sequence.to_bytes())
        datagram.extend(self.ack.to_bytes())
        datagram.extend(int(self.ack_bitfield, 2).to_bytes(4, 'big'))
        # The header makes up the first 12 bytes of the package
        if self.payload is not None:
            datagram.extend(self.payload)
        datagram = bytes(datagram)
        if len(datagram) > self.max_size:
            raise OverflowError('package exceeds the maximum size of ' + self.max_size + ' bytes')
        return bytes(datagram)

    @classmethod
    def from_datagram(cls, datagram: bytes):
        '''
        ### Arguments
         - **datagram** *bytes*: bytestring data, typically received via a socket
        
        ### Returns
        *Package*: the package from which the datagram has been created using `to_datagram()`

        ### Raises
         - **ProtocolIDMismatchError**: if the first four bytes don't match the Pygase protocol ID
        '''
        if datagram[:4] != cls._protocol_id:
            raise ProtocolIDMismatchError
        sequence = sqn.from_bytes(datagram[4:6])
        ack = sqn.from_bytes(datagram[6:8])
        ack_bitfield = bin(int.from_bytes(datagram[8:12], 'big'))[2:].zfill(32)
        if len(datagram) > 12:
            payload = datagram[12:]
        else:
            payload = None
        return Package(sequence, ack, ack_bitfield, payload)

class ConnectionStatus(NamedEnum):
    pass
ConnectionStatus.register('Disconnected')
ConnectionStatus.register('Connected')
ConnectionStatus.register('Connecting')

class Connection:
    '''
    This class resembles a client-server connection via the Pygase protocol.

    ### Arguments
     - **remote_address** *(str, int)*: A tuple `('hostname', port)` *required*

    ### Attributes
     - **local_sequence** *int*: sequence number of the last sent package
     - **remote_sequence** *int*: sequence number of the last received package
    A sequence of `0` means no packages have been sent or received.
    After `65535` sequence numbers wrap around to `1`, so they can be stored in 2 bytes.
     - **ack_bitfield** *str*: A 32 character string representing the 32 packages prior to `remote_sequence`,
        with the first character corresponding the packge directly preceding it and so forth.
        `'1'` means the package has been received, `'0'` means it hasn't.
     - **latency**: the last registered RTT (round trip time)
     - **status** *int*: A **ConnectionStatus** value.
     - **remote_address** *(str, int)*: A tuple `('hostname', port)`
    '''

    _package_intervals = {
        'good': 1/40,
        'bad': 1/20
    }
    timeout = 5 # connection timeout in seconds

    def __init__(self, remote_address):
        self.remote_address = remote_address
        self.local_sequence = sqn(0)
        self.remote_sequence = sqn(0)
        self.ack_bitfield = '0'*32
        self.latency = None
        self.status = ConnectionStatus.get('Connecting')
        self.quality = 'good'
        self._pending_acks = {}
        self._last_recv = time.time()
        self._latency_threshold = 0.25 # latency that will throttle the connection

    def recv(self, received_package: Package):
        '''
        Updates `remote_sequence` and `ack_bitfield` based on a received package.
        
        ### Raises
         - **DuplicateSequenceError**: if a package with the same sequence has already been received
        '''
        self._last_recv = time.time()
        self.status = ConnectionStatus.get('Connected')
        if self.remote_sequence == 0:
            self.remote_sequence = received_package.sequence
            return
        sequence_diff = self.remote_sequence - received_package.sequence
        if sequence_diff < 0:
            self.remote_sequence = received_package.sequence
            if sequence_diff == -1:
                self.ack_bitfield = '1' + self.ack_bitfield[:-1]
            else:
                self.ack_bitfield = self.ack_bitfield[:sequence_diff].zfill(32)
        if sequence_diff == 0:
            raise DuplicateSequenceError
        elif sequence_diff > 0:
            if self.ack_bitfield[sequence_diff-1] == '1':
                raise DuplicateSequenceError
            else:
                self.ack_bitfield = self.ack_bitfield[:sequence_diff-1] + '1' + self.ack_bitfield[sequence_diff:]
        # resolve pending acks for sent packages
        for pending_sequence in list(self._pending_acks):
            sequence_diff = received_package.ack - pending_sequence
            if sequence_diff < 0:
                continue
            elif sequence_diff == 0 or (sequence_diff <= 32 and received_package.ack_bitfield[sequence_diff] == '1'):
                self.update_latency(time.time() - self._pending_acks[pending_sequence])
                del self._pending_acks[pending_sequence]
            elif time.time() - self._pending_acks[pending_sequence] > Package.timeout:
                del self._pending_acks[pending_sequence]
                # package loss should be dealt with here

    async def send_loop(self, socket):
        '''
        Coroutine that, once spawned, will keep sending packages to the remote_address until it is explicitly
        cancelled or the connection times out.
        '''
        congestion_avoidance_monitor_task = await curio.spawn(self._congestion_avoidance_monitor)
        while True:
            t0 = time.time()
            if t0 - self._last_recv > self.timeout:
                self.status = ConnectionStatus.get('Disconnected')
                break
            await self._send_next_package(socket)
            await curio.sleep(max([self._package_intervals[self.quality] - time.time() + t0, 0]))
        congestion_avoidance_monitor_task.cancel()

    async def _send_next_package(self, socket):
        self.local_sequence += 1
        package = Package(self.local_sequence, self.remote_sequence, self.ack_bitfield)
        await socket.sendto(package.to_datagram(), self.remote_address)
        self._pending_acks[package.sequence] = time.time()

    async def _congestion_avoidance_monitor(self):
        while True:
            if self.latency is not None:
                if self.latency > self._latency_threshold and self.quality == 'good':
                    self.quality = 'bad'
                elif self.latency < self._latency_threshold and self.quality == 'bad':
                    self.quality = 'good'
            curio.sleep(1)

    def set_status(self, status: str):
        self.status = ConnectionStatus.get(status)

    def update_latency(self, rtt: int):
        if self.latency is None:
            self.latency == rtt
        else:
            # smoothed moving average to filter out network jitter
            self.latency += 0.1 * (rtt - self.latency)
