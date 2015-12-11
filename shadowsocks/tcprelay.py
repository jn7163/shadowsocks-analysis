#!/usr/bin/python
# -*- coding: utf-8 -*-
'''
实现tcp的转达，用在远程端中使远程和dest连接
'''


from __future__ import absolute_import, division, print_function, \
    with_statement

import time
import socket
# 处理Error Number的package
import errno
import struct
import logging
import traceback
import random

from shadowsocks import encrypt, eventloop, utils, common
from shadowsocks.common import parse_header

# we clear at most TIMEOUTS_CLEAN_SIZE timeouts each time
TIMEOUTS_CLEAN_SIZE = 512

# we check timeouts every TIMEOUT_PRECISION seconds
TIMEOUT_PRECISION = 4

# fast open 连接消息内容，在_handle_stage_connecting方法内
MSG_FASTOPEN = 0x20000000

# SOCKS CMD defination
CMD_CONNECT = 1
CMD_BIND = 2
CMD_UDP_ASSOCIATE = 3

# handler放在我们自己的服务器

# TCP Relay can be either sslocal or ssserver
# for sslocal it is called is_local=True

# for each opening port, we have a TCP Relay
# for each connection, we have a TCP Relay Handler to handle the connection

# for each handler, we have 2 sockets:
#    local:   connected to the client
#    remote:  connected to remote server

# for each handler, we have 2 streams:
#    upstream:    from client to server direction
#                 read local and write to remote
#    downstream:  from server to client direction
#                 read remote and write to local

# for each handler, it could be at one of several stages:

# stages?

# sslocal:
# stage 0 SOCKS hello received from local, send hello to local
# stage 1 addr received from local, query DNS for remote
# stage 2 UDP assoc
# stage 3 DNS resolved, connect to remote
# stage 4 still connecting, more data from local received
# stage 5 remote connected, piping local and remote

# ssserver:
# stage 0 just jump to stage 1
# stage 1 addr received from local, query DNS for remote
# stage 3 DNS resolved, connect to remote
# stage 4 still connecting, more data from local received
# stage 5 remote connected, piping local and remote


# TCP转发能在local和server端进行部署，用"is_local=True"进行区分两者
# 对于每一个打开的端口，能进行一个tcp转发，对于每一个连接，能进行一个tcp转发处理该连接
# 
# 对于每一个开放端口：有2个套接字：
# local: 连接到客户端  remote: 连接到远程服务器
# 对于每一个处理函数：有2个流：
# 上游：客户端->服务端（读写）
# 下游：服务端->客户端（读写）
# 
# 对每一个处理函数，由以下几个阶段组成：
# ss-local：
# 0： 本地的socks端口向本地端say hello
# 1： 本地socks端口收到dns请求，并向服务端查询dns with udp
# 2： 本地端收到udp
# 3： 返回dns结果，连接到服务端
# 4： 仍然保持连接，本地端继续接收服务端的数据
# 5： 连接到服务端，建立通信通道
# 
# ss-server：
# 0： 无
# 1： 本地socks端口收到dns请求，并向服务端查询dns
# 2： 返回dns结果，连接到境外网站（例如google.com）
# 3： 仍然保持连接，服务端端继续接收客户端的数据
# 4： 连接到客户端，建立通信通道


# 状态机
STAGE_INIT = 0
STAGE_ADDR = 1
STAGE_UDP_ASSOC = 2
STAGE_DNS = 3
STAGE_CONNECTING = 4
STAGE_STREAM = 5
STAGE_DESTROYED = -1

# stream direction
# 数据流方向
STREAM_UP = 0    # 上游：客户端->服务端
STREAM_DOWN = 1    # 下游：服务端->客户端

# stream wait status, indicating it's waiting for reading, etc
# 流的状态
WAIT_STATUS_INIT = 0
WAIT_STATUS_READING = 1
WAIT_STATUS_WRITING = 2
WAIT_STATUS_READWRITING = WAIT_STATUS_READING | WAIT_STATUS_WRITING

# 缓冲区
BUF_SIZE = 32 * 1024


class TCPRelayHandler(object):
    def __init__(self, server, fd_to_handlers, loop, local_sock, config,
                 dns_resolver, is_local):
        self._server = server
        self._fd_to_handlers = fd_to_handlers
        self._loop = loop
        self._local_sock = local_sock
        self._remote_sock = None
        self._config = config
        self._dns_resolver = dns_resolver
        self._is_local = is_local
        # 初始化
        self._stage = STAGE_INIT
        self._encryptor = encrypt.Encryptor(config['password'],
                                            config['method'])
        self._fastopen_connected = False
        self._data_to_write_to_local = []
        self._data_to_write_to_remote = []
        self._upstream_status = WAIT_STATUS_READING
        self._downstream_status = WAIT_STATUS_INIT
        self._remote_address = None
        if is_local:
            self._chosen_server = self._get_a_server()
        # 指定一个file描述符
        fd_to_handlers[local_sock.fileno()] = self
        # 非阻塞
        local_sock.setblocking(False)
        # TCP_NODELAY选项禁止Nagle算法。
        # Nagle算法通过将未确认的数据存入缓冲区直到蓄足一个包一起发送的方法，来减少主机发送的零碎小数据包的数目。
        local_sock.setsockopt(socket.SOL_TCP, socket.TCP_NODELAY, 1)
        # loop的定义在哪里，怎么就有一个add方法？
        loop.add(local_sock, eventloop.POLL_IN | eventloop.POLL_ERR)
        
        self.last_activity = 0
        self._update_activity()

    def __hash__(self):
        # default __hash__ is id / 16
        # we want to eliminate collisions（消除碰撞）
        return id(self)

    # 对于类的方法，装饰器起作用返回一个调用。@property装饰器就是负责把一个方法变成属性调用
    @property
    def remote_address(self):
        return self._remote_address

    def _get_a_server(self):
        server = self._config['server']
        server_port = self._config['server_port']
        # 随机挑选一个服务端
        if type(server_port) == list:
            server_port = random.choice(server_port)
        logging.debug('chosen server: %s:%d', server, server_port)
        # TODO support multiple server IP
        return server, server_port

    def _update_activity(self):
        # tell the TCP Relay we have activities recently
        # else it will think we are inactive and timed out
        # 告诉TCP转发器：“最近有活跃的链接”，否则超时
        self._server.update_activity(self)

    def _update_stream(self, stream, status):
        # update a stream to a new waiting status
        # 更新流：进入新的等待状态
        # check if status is changed
        # only update if dirty
        # 检查状态是否被改变，只在dirty时候更新
        # 不太理解。。
        dirty = False
        if stream == STREAM_DOWN:
            if self._downstream_status != status:
                self._downstream_status = status
                dirty = True
        elif stream == STREAM_UP:
            if self._upstream_status != status:
                self._upstream_status = status
                dirty = True
        if dirty:
            if self._local_sock:
                event = eventloop.POLL_ERR
                if self._downstream_status & WAIT_STATUS_WRITING:
                    event |= eventloop.POLL_OUT
                if self._upstream_status & WAIT_STATUS_READING:
                    event |= eventloop.POLL_IN
                self._loop.modify(self._local_sock, event)
            if self._remote_sock:
                event = eventloop.POLL_ERR
                if self._downstream_status & WAIT_STATUS_READING:
                    event |= eventloop.POLL_IN
                if self._upstream_status & WAIT_STATUS_WRITING:
                    event |= eventloop.POLL_OUT
                self._loop.modify(self._remote_sock, event)

    def _write_to_sock(self, data, sock):
        # write data to sock
        # if only some of the data are written, put remaining in the buffer
        # and update the stream to wait for writing
        # 写入数据到套接字，如果只有部分数据被写入，继续写入剩下的数据到缓冲区，并更新流方向为‘等待’
        if not data or not sock:
            return False
        uncomplete = False
        try:
            l = len(data)
            s = sock.send(data)
            if s < l:
                # 返回list的切片，应该不是浅复制
                data = data[s:]
                uncomplete = True
        except (OSError, IOError) as e:
            error_no = eventloop.errno_from_exception(e)
            if error_no in (errno.EAGAIN, errno.EINPROGRESS,
                            errno.EWOULDBLOCK):
                uncomplete = True
            else:
                logging.error(e)
                # 哆嗦模式
                if self._config['verbose']:
                    traceback.print_exc()
                # 断开连接
                self.destroy()
                # 尚未发送完毕
                return False
        # 尚未完成发送数据
        if uncomplete:
            if sock == self._local_sock:
                # 暂存数据，使用append追加
                self._data_to_write_to_local.append(data)
                # 更新流的状态：等待写入，方向为‘服务端->本地’
                self._update_stream(STREAM_DOWN, WAIT_STATUS_WRITING)
            elif sock == self._remote_sock:
                # 跟上面一样的方法，追加数据，修改stream状态。
                self._data_to_write_to_remote.append(data)
                # 方向‘本地->服务端’
                self._update_stream(STREAM_UP, WAIT_STATUS_WRITING)
            else:
                logging.error('write_all_to_sock:unknown socket')
        # 已经完成发送数据
        else:
            if sock == self._local_sock:
                # 修改流的状态为等待读，方向为‘服务端->本地’
                self._update_stream(STREAM_DOWN, WAIT_STATUS_READING)
            elif sock == self._remote_sock:
                # 方向为‘本地->服务端’
                self._update_stream(STREAM_UP, WAIT_STATUS_READING)
            else:
                logging.error('write_all_to_sock:unknown socket')
        # 函数执行完毕，不一定说明数据发送完毕，return true
        return True

    # 连接stage的处理，即对fast_open的处理
    def _handle_stage_connecting(self, data):
        # 如果是本地端，加密数据
        if self._is_local:
            data = self._encryptor.encrypt(data)
        self._data_to_write_to_remote.append(data)
        # 若本地端设置了fast_open却没有fast_open连接
        if self._is_local and not self._fastopen_connected and \
                self._config['fast_open']:
            # for sslocal and fastopen, we basically wait for data and use
            # sendto to connect
            try:
                # only connect once
                self._fastopen_connected = True
                remote_sock = \
                    self._create_remote_socket(self._chosen_server[0],
                                               self._chosen_server[1])
                self._loop.add(remote_sock, eventloop.POLL_ERR)
                # 发送二进制流
                data = b''.join(self._data_to_write_to_local)
                l = len(data)
                # 发送给服务端
                s = remote_sock.sendto(data, MSG_FASTOPEN, self._chosen_server)
                # 若发送尚未完成，转入读写状态
                if s < l:
                    data = data[s:]
                    self._data_to_write_to_local = [data]
                    self._update_stream(STREAM_UP, WAIT_STATUS_READWRITING)
                # 发送完毕，转入读状态
                else:
                    self._data_to_write_to_local = []
                    self._update_stream(STREAM_UP, WAIT_STATUS_READING)
                    self._stage = STAGE_STREAM
            except (OSError, IOError) as e:
                # EINPROGRESS错误,表示连接操作正在进行中,但是仍未完成,常见于非阻塞的socket连接中
                # stream流状态更新为读写
                if eventloop.errno_from_exception(e) == errno.EINPROGRESS:
                    self._update_stream(STREAM_UP, WAIT_STATUS_READWRITING)
                # ENOTCONN指定的socket是一个未连接成功的socket
                elif eventloop.errno_from_exception(e) == errno.ENOTCONN:
                    logging.error('fast open not supported on this OS')
                    self._config['fast_open'] = False
                    self.destroy()
                else:
                    logging.error(e)
                    if self._config['verbose']:
                        traceback.print_exc()
                    self.destroy()
                    
    # dns远端解析stage的处理函数
    def _handle_stage_addr(self, data):
        try:
            if self._is_local:
                cmd = common.ord(data[1])
                if cmd == CMD_UDP_ASSOCIATE:
                    logging.debug('UDP associate')
                    # 打包header，对v6和v4地址判断
                    if self._local_sock.family == socket.AF_INET6:
                        header = b'\x05\x00\x00\x04'
                    else:
                        header = b'\x05\x00\x00\x01'
                    addr, port = self._local_sock.getsockname()[:2]    # 应该是返回一个元组吧，怎么返回一个列表，含有两个元素
                    addr_to_send = socket.inet_pton(self._local_sock.family,
                                                    addr)
                    port_to_send = struct.pack('>H', port)
                    # 不太理解数据发送到哪里》是本地吗，还是远端？
                    self._write_to_sock(header + addr_to_send + port_to_send,
                                        self._local_sock)
                    self._stage = STAGE_UDP_ASSOC
                    # just wait for the client to disconnect
                    # 返回，只需等待客户端断开。（应该是等待socks端口断开）
                    return
                # 连接命令，马上进行连接查询dns
                elif cmd == CMD_CONNECT:
                    # just trim VER CMD RSV
                    data = data[3:]
                else:
                    logging.error('unknown command %d', cmd)
                    self.destroy()
                    return
            # parse_header是common.py中的解开header函数
            header_result = parse_header(data)
            if header_result is None:
                raise Exception('can not parse header')
            addrtype, remote_addr, remote_port, header_length = header_result
            logging.info('connecting %s:%d' % (common.to_str(remote_addr),
                                               remote_port))
            self._remote_address = (remote_addr, remote_port)
            # 暂停读取，改为等待向上游写入。
            self._update_stream(STREAM_UP, WAIT_STATUS_WRITING)
            # 状态机转为查询dns
            self._stage = STAGE_DNS
            if self._is_local:
                # 我觉得是给本地的socks：1080一个简单的答复吧。
                self._write_to_sock((b'\x05\x00\x00\x01'
                                     b'\x00\x00\x00\x00\x10\x10'),
                                    self._local_sock)
                # 加密内容
                data_to_send = self._encryptor.encrypt(data)
                # 向服务端端查询
                self._data_to_write_to_remote.append(data_to_send)
                # notice here may go into _handle_dns_resolved directly
                # 这里调用DNSResolver类的resolve方法
                # 这里跳转得有点多。。绕了一圈
                self._dns_resolver.resolve(self._chosen_server[0],
                                           self._handle_dns_resolved)
            else:
                if len(data) > header_length:
                    self._data_to_write_to_remote.append(data[header_length:])
                # notice here may go into _handle_dns_resolved directly
                self._dns_resolver.resolve(remote_addr,
                                           self._handle_dns_resolved)
        except Exception as e:
            logging.error(e)
            if self._config['verbose']:
                traceback.print_exc()
            # TODO use logging when debug completed
            self.destroy()

    def _create_remote_socket(self, ip, port):
        addrs = socket.getaddrinfo(ip, port, 0, socket.SOCK_STREAM,
                                   socket.SOL_TCP)
        if len(addrs) == 0:
            raise Exception("getaddrinfo failed for %s:%d" % (ip, port))
        af, socktype, proto, canonname, sa = addrs[0]
        remote_sock = socket.socket(af, socktype, proto)
        self._remote_sock = remote_sock
        self._fd_to_handlers[remote_sock.fileno()] = self
        remote_sock.setblocking(False)
        remote_sock.setsockopt(socket.SOL_TCP, socket.TCP_NODELAY, 1)
        return remote_sock

    # 处理dns解析结果
    def _handle_dns_resolved(self, result, error):
        if error:
            logging.error(error)
            self.destroy()
            return
        if result:
            ip = result[1]
            if ip:
                try:
                    # 状态机转为：连接中
                    self._stage = STAGE_CONNECTING
                    remote_addr = ip
                    if self._is_local:
                        remote_port = self._chosen_server[1]
                    else:
                        remote_port = self._remote_address[1]

                    if self._is_local and self._config['fast_open']:
                        # for fastopen:
                        # wait for more data to arrive and send them in one SYN
                        self._stage = STAGE_CONNECTING
                        # we don't have to wait for remote since it's not
                        # created
                        self._update_stream(STREAM_UP, WAIT_STATUS_READING)
                        # TODO when there is already data in this packet
                    else:
                        # else do connect
                        remote_sock = self._create_remote_socket(remote_addr,
                                                                 remote_port)
                        try:
                            remote_sock.connect((remote_addr, remote_port))
                        except (OSError, IOError) as e:
                            if eventloop.errno_from_exception(e) == \
                                    errno.EINPROGRESS:
                                pass
                        self._loop.add(remote_sock,
                                       eventloop.POLL_ERR | eventloop.POLL_OUT)
                        self._stage = STAGE_CONNECTING
                        self._update_stream(STREAM_UP, WAIT_STATUS_READWRITING)
                        self._update_stream(STREAM_DOWN, WAIT_STATUS_READING)
                    return
                except (OSError, IOError) as e:
                    logging.error(e)
                    if self._config['verbose']:
                        traceback.print_exc()
        self.destroy()

    def _on_local_read(self):
        # handle all local read events and dispatch them to methods for
        # each stage
        self._update_activity()
        if not self._local_sock:
            return
        is_local = self._is_local
        data = None
        try:
            data = self._local_sock.recv(BUF_SIZE)
        except (OSError, IOError) as e:
            if eventloop.errno_from_exception(e) in \
                    (errno.ETIMEDOUT, errno.EAGAIN, errno.EWOULDBLOCK):
                return
        if not data:
            self.destroy()
            return
        if not is_local:
            data = self._encryptor.decrypt(data)
            if not data:
                return
        if self._stage == STAGE_STREAM:
            if self._is_local:
                data = self._encryptor.encrypt(data)
            self._write_to_sock(data, self._remote_sock)
            return
        elif is_local and self._stage == STAGE_INIT:
            # TODO check auth method
            self._write_to_sock(b'\x05\00', self._local_sock)
            self._stage = STAGE_ADDR
            return
        elif self._stage == STAGE_CONNECTING:
            self._handle_stage_connecting(data)
        elif (is_local and self._stage == STAGE_ADDR) or \
                (not is_local and self._stage == STAGE_INIT):
            self._handle_stage_addr(data)

    def _on_remote_read(self):
        # handle all remote read events
        self._update_activity()
        data = None
        try:
            data = self._remote_sock.recv(BUF_SIZE)
        except (OSError, IOError) as e:
            if eventloop.errno_from_exception(e) in \
                    (errno.ETIMEDOUT, errno.EAGAIN, errno.EWOULDBLOCK):
                return
        if not data:
            self.destroy()
            return
        if self._is_local:
            data = self._encryptor.decrypt(data)
        else:
            data = self._encryptor.encrypt(data)
        try:
            self._write_to_sock(data, self._local_sock)
        except Exception as e:
            logging.error(e)
            if self._config['verbose']:
                traceback.print_exc()
            # TODO use logging when debug completed
            self.destroy()

    def _on_local_write(self):
        # handle local writable event
        if self._data_to_write_to_local:
            data = b''.join(self._data_to_write_to_local)
            self._data_to_write_to_local = []
            self._write_to_sock(data, self._local_sock)
        else:
            self._update_stream(STREAM_DOWN, WAIT_STATUS_READING)

    def _on_remote_write(self):
        # handle remote writable event
        self._stage = STAGE_STREAM
        if self._data_to_write_to_remote:
            data = b''.join(self._data_to_write_to_remote)
            self._data_to_write_to_remote = []
            self._write_to_sock(data, self._remote_sock)
        else:
            self._update_stream(STREAM_UP, WAIT_STATUS_READING)

    def _on_local_error(self):
        logging.debug('got local error')
        if self._local_sock:
            logging.error(eventloop.get_sock_error(self._local_sock))
        self.destroy()

    def _on_remote_error(self):
        logging.debug('got remote error')
        if self._remote_sock:
            logging.error(eventloop.get_sock_error(self._remote_sock))
        self.destroy()

    # key
    def handle_event(self, sock, event):
        # handle all events in this handler and dispatch them to methods
        if self._stage == STAGE_DESTROYED:
            logging.debug('ignore handle_event: destroyed')
            return
        # order is important
        if sock == self._remote_sock:
            if event & eventloop.POLL_ERR:
                # remote就是墙外的服务器
                self._on_remote_error()
                if self._stage == STAGE_DESTROYED:
                    return
            if event & (eventloop.POLL_IN | eventloop.POLL_HUP):
                self._on_remote_read()
                if self._stage == STAGE_DESTROYED:
                    return
            if event & eventloop.POLL_OUT:
                self._on_remote_write()
        elif sock == self._local_sock:
            if event & eventloop.POLL_ERR:
                # local就是我们墙内的proxy
                self._on_local_error()
                if self._stage == STAGE_DESTROYED:
                    return
            if event & (eventloop.POLL_IN | eventloop.POLL_HUP):
                self._on_local_read()
                if self._stage == STAGE_DESTROYED:
                    return
            if event & eventloop.POLL_OUT:
                self._on_local_write()
        else:
            logging.warn('unknown socket')

    def destroy(self):
        # destroy the handler and release any resources
        # promises:
        # 1. destroy won't make another destroy() call inside
        # 2. destroy releases resources so it prevents future call to destroy
        # 3. destroy won't raise any exceptions
        # if any of the promises are broken, it indicates a bug has been
        # introduced! mostly likely memory leaks, etc
        if self._stage == STAGE_DESTROYED:
            # this couldn't happen
            logging.debug('already destroyed')
            return
        self._stage = STAGE_DESTROYED
        if self._remote_address:
            logging.debug('destroy: %s:%d' % 
                          self._remote_address)
        else:
            logging.debug('destroy')
        if self._remote_sock:
            logging.debug('destroying remote')
            self._loop.remove(self._remote_sock)
            del self._fd_to_handlers[self._remote_sock.fileno()]
            self._remote_sock.close()
            self._remote_sock = None
        if self._local_sock:
            logging.debug('destroying local')
            self._loop.remove(self._local_sock)
            del self._fd_to_handlers[self._local_sock.fileno()]
            self._local_sock.close()
            self._local_sock = None
        self._dns_resolver.remove_callback(self._handle_dns_resolved)
        self._server.remove_handler(self)


class TCPRelay(object):
    def __init__(self, config, dns_resolver, is_local):
        self._config = config
        self._is_local = is_local
        self._dns_resolver = dns_resolver
        self._closed = False
        self._eventloop = None
        self._fd_to_handlers = {}
        self._last_time = time.time()

        self._timeout = config['timeout']
        self._timeouts = []    # a list for all the handlers
        # we trim the timeouts once a while
        self._timeout_offset = 0    # last checked position for timeout
        self._handler_to_timeouts = {}    # key: handler value: index in timeouts

        if is_local:
            listen_addr = config['local_address']
            listen_port = config['local_port']
        else:
            listen_addr = config['server']
            listen_port = config['server_port']
        self._listen_port = listen_port

        # (family, type, proto, canonname, sockaddr)
        addrs = socket.getaddrinfo(listen_addr, listen_port, 0,
                                   socket.SOCK_STREAM, socket.SOL_TCP)
        if len(addrs) == 0:
            raise Exception("can't get addrinfo for %s:%d" % 
                            (listen_addr, listen_port))

        af, socktype, proto, canonname, sa = addrs[0]
        server_socket = socket.socket(af, socktype, proto)
        server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server_socket.bind(sa)
        server_socket.setblocking(False)
        if config['fast_open']:
            try:
                server_socket.setsockopt(socket.SOL_TCP, 23, 5)
            except socket.error:
                logging.error('warning: fast open is not available')
                self._config['fast_open'] = False
        
        # it is a server in the sense of browser
        server_socket.listen(1024)
        self._server_socket = server_socket

    def add_to_loop(self, loop):
        if self._eventloop:
            raise Exception('already add to loop')
        if self._closed:
            raise Exception('already closed')
        self._eventloop = loop

        # 这里就相比dnsserver少了一层
        loop.add_handler(self._handle_events)

        self._eventloop.add(self._server_socket,
                            eventloop.POLL_IN | eventloop.POLL_ERR)

    def remove_handler(self, handler):
        index = self._handler_to_timeouts.get(hash(handler), -1)
        if index >= 0:
            # delete is O(n), so we just set it to None
            self._timeouts[index] = None
            del self._handler_to_timeouts[hash(handler)]

    def update_activity(self, handler):
        # set handler to active
        now = int(time.time())
        if now - handler.last_activity < TIMEOUT_PRECISION:
            # thus we can lower timeout modification frequency
            return
        handler.last_activity = now
        index = self._handler_to_timeouts.get(hash(handler), -1)
        if index >= 0:
            # delete is O(n), so we just set it to None
            self._timeouts[index] = None
        length = len(self._timeouts)
        self._timeouts.append(handler)
        self._handler_to_timeouts[hash(handler)] = length

    def _sweep_timeout(self):
        # tornado's timeout memory management is more flexible than we need
        # we just need a sorted last_activity queue and it's faster than heapq
        # in fact we can do O(1) insertion/remove so we invent our own
        if self._timeouts:
            logging.log(utils.VERBOSE_LEVEL, 'sweeping timeouts')
            now = time.time()
            length = len(self._timeouts)
            pos = self._timeout_offset
            while pos < length:
                handler = self._timeouts[pos]
                if handler:
                    if now - handler.last_activity < self._timeout:
                        break
                    else:
                        if handler.remote_address:
                            logging.warn('timed out: %s:%d' % 
                                         handler.remote_address)
                        else:
                            logging.warn('timed out')
                        handler.destroy()
                        self._timeouts[pos] = None    # free memory
                        pos += 1
                else:
                    pos += 1
            if pos > TIMEOUTS_CLEAN_SIZE and pos > length >> 1:
                # clean up the timeout queue when it gets larger than half
                # of the queue
                self._timeouts = self._timeouts[pos:]
                for key in self._handler_to_timeouts:
                    self._handler_to_timeouts[key] -= pos
                pos = 0
            self._timeout_offset = pos

    # 处理从dest传回到远程的消息
    def _handle_events(self, events):
        # handle events and dispatch to handlers
        for sock, fd, event in events:
            if sock:
                logging.log(utils.VERBOSE_LEVEL, 'fd %d %s', fd,
                            eventloop.EVENT_NAMES.get(event, event))
            if sock == self._server_socket:
                if event & eventloop.POLL_ERR:
                    # TODO
                    raise Exception('server_socket error')
                try:
                    logging.debug('accept')
                    conn = self._server_socket.accept()
                    # 创建一个新的连接，并且新建一个TCPRelayHandler处理
                    TCPRelayHandler(self, self._fd_to_handlers,
                                    self._eventloop, conn[0], self._config,
                                    self._dns_resolver, self._is_local)
                except (OSError, IOError) as e:
                    error_no = eventloop.errno_from_exception(e)
                    if error_no in (errno.EAGAIN, errno.EINPROGRESS,
                                    errno.EWOULDBLOCK):
                        continue
                    else:
                        logging.error(e)
                        if self._config['verbose']:
                            traceback.print_exc()
            else:
                if sock:
                    # 如果是已经accept的连接，就找相对应的handler处理它
                    handler = self._fd_to_handlers.get(fd, None)
                    if handler:
                        # 这里调用handler里面的handle_event来处理事件
                        handler.handle_event(sock, event)
                else:
                    logging.warn('poll removed fd')

        now = time.time()
        if now - self._last_time > TIMEOUT_PRECISION:
            self._sweep_timeout()
            self._last_time = now
        if self._closed:
            if self._server_socket:
                self._eventloop.remove(self._server_socket)
                self._server_socket.close()
                self._server_socket = None
                logging.info('closed listen port %d', self._listen_port)
            if not self._fd_to_handlers:
                self._eventloop.remove_handler(self._handle_events)

    def close(self, next_tick = False):
        self._closed = True
        if not next_tick:
            self._server_socket.close()
