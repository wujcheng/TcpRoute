#!/usr/bin/python
# -*- coding: utf-8 -*-
# utf-8 中文编码

u''' TCP 路由器

'''

# @2013.08.22 by felix021
# This file is modified fron github/felix021/mixo
# to act as a pure socks5 proxy.
#
# usage:
#    python msocks5.py          #listens on 7070
#    python msocks5.py 1080     #listens on 1080
import os
import sys
import struct
import signal
import threading
import time
import logging
import traceback
from LRUCacheDict import LRUCacheDict

try:
    import gevent
    from gevent import socket
    from gevent.server import StreamServer
    from gevent.pool import Group
except:
    print >>sys.stderr, "please install gevent first!"
    sys.exit(1)

try:
    import dns.resolver
except:
    print >>sys.stderr, 'please install dnspython !'

# 悲剧，windows python 3.4 才支持 ipv6 的 inet_ntop
# https://bugs.python.org/issue7171
if not socket.__dict__.has_key('inet_ntop'):
    from win_inet_pton import inet_ntop
    socket.inet_ntop = inet_ntop
if not socket.__dict__.has_key("inet_pton"):
    from win_inet_pton import inet_pton
    socket.inet_pton = inet_pton

logging.basicConfig(level=logging.DEBUG)
basedir = os.path.abspath(os.path.dirname(__file__))


getaddrinfoLock = threading.Lock()
def getaddrinfo(hostname,port):
    global gfwIP
    try:
        for i in range(5):
            res = socket.getaddrinfo(hostname, port,0,socket.SOCK_STREAM,socket.IPPROTO_TCP)
            with getaddrinfoLock:
                for r in res:
                    if not gfwIP.has_key(r[4][0]):
                        return res
                    else:
                        logging.info('[DNS]%s(%s) ip in gfwIP !'%(hostname,r[4][0]))
                        break
    except socket.gaierror as e:
        pass
    return []

def getAddrinfoLoop():
    try:
        import dns.resolver
    except:
        return
    while True:
        logging.info('gfwIP loop start')
        _gfwIP = {}
        m = dns.resolver.Resolver()
        m.nameservers=['8.8.8.123',]
        for i in range(100):
            for a in m.query('twitter.com').response.answer:
                for r in a:
                    _gfwIP[r.address]=int(time.time()*1000)
            time.sleep(0.1)
        with getaddrinfoLock:
             global gfwIP
             gfwIP=_gfwIP
        logging.info('gfwIP:\r\n' + '\r\n'.join(gfwIP))

        time.sleep(1*60*60)


# 源客户端
class SClient:
    u'''每个代理请求会生成一个源客户端。'''
    def __init__(self,server,conn, address):
        self.server =server
        self.conn =conn
        self.sAddress = address
        self.connected = False

    def unpack(self, fmt):
        length = struct.calcsize(fmt)
        data = self.conn.recv(length)
        if len(data) < length:
            raise Exception("SClient.unpack: bad formatted stream")
        return struct.unpack(fmt, data)

    def pack(self, fmt, *args):
        data = struct.pack(fmt, *args)
        return self.conn.sendall(data)

    def handle(self):

        # 获得请求并发出新的请求。
        (ver,) = self.unpack('B')
        if ver ==0x05:
            # socket5 协议
            logging.debug('Receive socket5 protocol header')
            self.socket5Handle(ver)
        elif chr(ver) in 'GgPpHhDdTtCcOo':
            # 误被当作 http 代理
            logging.error('Receive http header')
            self.httpHandle()
        else:
            # 未知的类型，以 socket5 协议拒绝
            logging.error('Receive an unknown protocol header')
            self.pack('BB',0x05,0xff)

    def isConnected(self):
        u''' 是否已连接到远程
如果已连接就不会再次通过新的代理服务器尝试建立连接。 '''
        return self.connected
    def setConnected(self,value):
        self.connected = value


    def socket5Handle(self,head):
        # 鉴定
        (nmethods,) = self.unpack('B')
        if nmethods>0:
            (methods,) = self.unpack('B'*nmethods)
            #TODO: 未检查客户端支持的鉴定方式
        self.pack('BB',0x05,0x00)
        logging.debug('client login')

        # 接收代理请求
        ver,cmd,rsv,atyp = self.unpack('BBBB')

        if ver != 0x05 or cmd != 0x01:
            self.pack('BBBBIH',0x05, 0x07, 0x00, 0x01, 0, 0)
            self.conn.close()
            return

        if atyp == 0x01:
            # ipv4
            host,port = self.unpack('!IH')
            hostname = socket.inet_ntoa(struct.pack('!I', host))
        elif atyp == 0x03:
            # 域名
            length = self.unpack('B')[0]
            hostname, port = self.unpack("!%dsH" % length)
        elif atyp == 0x04:
            # ipv6
            ipv61 ,ipv62,port = self.unpack('!2QH')
            hostname = socket.inet_ntop(socket.AF_INET6, struct.pack('!2Q', ipv61, ipv62))
        else:
            self.pack('!BBBBIH', 0x05, 0x07, 0x00, 0x01, 0, 0)
            self.conn.close()
            return
        logging.debug('[Request] host:%s   prot:%s'%(hostname,port))

        # 对外发起请求

        proxyDict = self.server.getProxyCache(hostname,port,None)
        if proxyDict:
            proxyList = sorted(proxyDict.values(),key=lambda x:x['tcpping'])
            proxyName = proxyList[0]['proxyName']
            hitIp = proxyList[0]['hitIp']
            proxy = self.server.getProxy(proxyName)
            if proxy:
                logging.debug('[Cache] hit host:%s ,prot:%s ,proxy:%s ,ip:%s'%(hostname,port,proxy.getName(),hitIp))
                proxy.forward(self,atyp,hostname,port,3,hitIp)
        if not self.connected:
            # 不管是没有缓存记录还是没连接上，都使用全部链接做一次测试。
            logging.debug('[all proxt]  host:%s ,prot:%s '%(hostname,port))
            group = Group()
            for proxy in self.server.getProxy():
                # 启动多个代理进行转发尝试
                # 最先成功的会执行转发，之后成功的会自动退出。
                group.add(gevent.spawn(proxy.forward,self,atyp,hostname,port,10))
            group.join()
        if not self.connected:
            self.pack('!BBBBIH', 0x05, 0x03, 0x00, 0x01, 0, port)
        self.conn.close()




    def httpHandle(self,head):
        self.conn.sendall('''HTTP/1.1 200 OK
Content-Type:text/html; charset=utf-8

<h1>HTTP agent is not supported</h1>
HTTP agent is not supported。''')






class DirectProxy():
    u'''直接连接'''
    def forward(self,sClient,atyp,hostname,port,timeout=socket._GLOBAL_DEFAULT_TIMEOUT,ip=None):
        u'''阻塞调用，'''
        logging.debug('DirectProxy.forward(%s,%s,%s,%s,%s)'%(atyp,hostname,port,timeout,ip))
        addrinfoList = getaddrinfo(hostname,port)
        logging.debug('[DNS]resolution name:%s\r\n'%hostname+'\r\n'.join([('IP:%s'%addrin[4][0]) for addrin in addrinfoList]))
        group = Group()
        if ip in [addrin[4][0] for addrin in addrinfoList]:
            logging.debug('cache ip hit Domain:%s ip:%s '%(hostname,ip))
            group.add(gevent.spawn(self.__forward,sClient,ip,port,hostname,timeout))
        else:
            for addrinfo in addrinfoList:
                # 启动多个代理进行转发尝试
                # 最先成功的会执行转发，之后成功的会自动退出。
                group.add(gevent.spawn(self.__forward,sClient,addrinfo[0],addrinfo[4],hostname,timeout))
        group.join()

    def __forward(self,sClient,atyp,addr,hostname,timeout=socket._GLOBAL_DEFAULT_TIMEOUT):
        logging.debug('DirectProxy.__forward(%s,%s,%s,%s,%s)'%(hostname,atyp,addr[0],addr[1],timeout))
        startTime = int(time.time()*1000)
        try:
            s = socket.create_connection(addr,timeout)
        except:
            #TODO: 处理下连接失败
            logging.debug('socket.create_connection err host:%s ,port:%s ,timeout:%s'%(addr[0],addr[1],timeout))
            return
        # 直连的话直接链接到服务器就可以，
        # 如果是 socket5 代理，时间统计需要包含远端代理服务器连接到远端服务器的时间。
        sClient.server.upProxyPing(self.getName(),hostname,addr[1],int(time.time()*1000)-startTime,addr[0])
        if not sClient.connected:
            # 第一个连接上的
            logging.debug('[DirectProxy] Connection hit (%s,%s,%s,%s,%s)'%(hostname,atyp,addr[0],addr[1],timeout))
            sClient.connected=True
            #TODO: 按照socket5协议，这里应该返回服务器绑定的地址及端口
            # http://blog.csdn.net/testcs_dn/article/details/7915505
            sClient.pack('!BBBBIH', 0x05, 0x00, 0x00, 0x01, 0, 0)
            # 第一个连接上的，执行转发
            group = Group()
            group.add(gevent.spawn(self.__forwardData,sClient.conn,s))
            group.add(gevent.spawn(self.__forwardData,s,sClient.conn))
            group.join()
        else:
            # 不是第一个连接上的
            s.close()
            logging.debug('[DirectProxy] Connection miss (%s,%s,%s,%s,%s)'%(hostname,atyp,addr[0],addr[1],timeout))


    def __forwardData(self,s,d):
        try:
            while True:
                data=s.recv(1024)
                if not data:
                    break
                d.sendall(data)
        except:
            logging.exception('DirectProxy.__forwardData')
        finally:
            # 这里 和 socket5Handle 会重复关闭
            logging.debug('DirectProxy.__forwardData  finally')
            s.close()
            d.close()


    def getName(self):
        u'''代理唯一名称
需要保证唯一性，建议使用 socket5-proxyhost:port 为代理名称。
'''
        return 'direct'



class Socket5Proxy():
    u'''Socket5'''
    def __init__(self,host,port):
        self.host = host
        self.port =port

    def unpack(self, fmt):
        length = struct.calcsize(fmt)
        data = self.s.recv(length)
        if len(data) < length:
            raise Exception("SClient.unpack: bad formatted stream")
        return struct.unpack(fmt, data)

    def pack(self, fmt, *args):
        data = struct.pack(fmt, *args)
        return self.s.sendall(data)

    def forward(self,sClient,atyp,hostname,port,timeout=socket._GLOBAL_DEFAULT_TIMEOUT,ip=None):
        u'''阻塞调用，'''
        logging.debug('Socket5Proxy.forward(%s,%s,%s,%s,%s)'%(atyp,hostname,port,timeout,ip))
        self.__forward(sClient,atyp,hostname,port,timeout)

    def __forward(self,sClient,atyp,hostname,port,timeout=socket._GLOBAL_DEFAULT_TIMEOUT):
        startTime = int(time.time()*1000)
        try:
            s = socket.create_connection((self.host,self.port),timeout)
        except:
            #TODO: 处理下连接失败
            logging.debug('[socket5]socket.create_connection err host:%s ,port:%s ,timeout:%s'%(self.host,self.port,timeout))
            return

        logging.debug('[socket5]socket.Connected  host:%s ,port:%s ,timeout:%s'%(self.host,self.port,timeout))

        # socket5 协议
        self.s=s
        # 登录
        self.pack('BBB',0x05,0x01,0x00)

        # 登录回应
        ver,method = self.unpack('BB')
        if ver != 0x05 or method != 0x00:
            logging.error('socket5 proxy password err .host:%s ,port:%s'%(self.host,self.port))
            self.s.close()
            return
        logging.debug('socket5 proxy login host:%s ,port:%s'%(self.host,self.port))

        # 请求连接
        self.pack('!BBBB',0x05,0x01,0x00,atyp)
        if atyp == 0x01:
            #ipv4
            self.pack('!IH',socket.inet_aton(hostname),port)
        elif atyp == 0x03:
            # 域名
            self.pack('!B%ssH'%len(hostname),len(hostname),hostname,port)
        elif atyp == 0x04:
            # ipv6
            _str = socket.inet_pton(socket.AF_INET6, hostname)
            a, b = struct.unpack('!2Q', _str)
            self.pack('!2QH',a,b,port)
        else:
            logging.error('Unknown atyp:%s'%atyp)
            self.s.close()
            return

        # 请求回应
        ver,rep,rsv,atyp = self.unpack('BBBB')
        if ver != 0x05 or rep != 0x00:
            logging.error('socket5 proxy  err,ver:%s ,rep:%s'%(ver,rep))
            self.s.close()
            return


        if atyp == 0x01:
            self.unpack('!IH')
        elif atyp == 0x03:
            length = self.unpack('B')
            self.unpack('%ssH'%length)
        elif atyp == 0x04:
            self.unpack('!2QH')

        # 直连的话直接链接到服务器就可以，
        # 如果是 socket5 代理，时间统计需要包含远端代理服务器连接到远端服务器的时间。
        sClient.server.upProxyPing(self.getName(),hostname,port,int(time.time()*1000)-startTime,None)
        if not sClient.connected:
            # 第一个连接上的
            logging.debug('[socket5Proxy] Connection hit (%s,%s,%s,%s)'%(hostname,atyp,port,timeout))
            sClient.connected=True
            #TODO: 按照socket5协议，这里应该返回服务器绑定的地址及端口
            # http://blog.csdn.net/testcs_dn/article/details/7915505
            sClient.pack('!BBBBIH', 0x05, 0x00, 0x00, 0x01, 0, 0)
            # 第一个连接上的，执行转发
            group = Group()
            group.add(gevent.spawn(self.__forwardData,sClient.conn,s))
            group.add(gevent.spawn(self.__forwardData,s,sClient.conn))
            group.join()
        else:
            # 不是第一个连接上的
            s.close()
            logging.debug('[socket5Proxy] Connection miss (%s,%s,%s,%s)'%(hostname,atyp,port,timeout))

    def __forwardData(self,s,d):
        try:
            while True:
                data=s.recv(1024)
                if not data:
                    break
                d.sendall(data)
        except:
            logging.exception('socket5Proxy.__forwardData')
        finally:
            # 这里 和 socket5Handle 会重复关闭
            logging.debug('socket5Proxy.__forwardData close()')
            s.close()
            d.close()


    def getName(self):
        u'''代理唯一名称
需要保证唯一性，建议使用 socket5-proxyhost:port 为代理名称。
'''
        return 'socket5-%s:%s'%(self.host,self.port)


class SocksServer(StreamServer):

    def __init__(self, listener):
        StreamServer.__init__(self, listener)
        self.proxyDict={}
        self.addProxy(DirectProxy())
        # 路由缓存格式
        # {
        #   %s-%s-%s'%(atyp,hostname,port) :
        #   {
        #       proxyName:
        #       {
        #           'tcpping':starTime - time()*1000,
        #           'proxyName': proxy.getName(),
        #           'hitIp' : '115.239.210.27'命中IP，在代理支持的情况下会使用。
        #       },
        #   }

        # }
        self.routeCache = LRUCacheDict(500,10*60*1000)

    def addProxy(self,proxy):
        logging.info('addProxy %s'%proxy.getName())
        self.proxyDict[proxy.getName()]=proxy

    def getProxy(self,name=None,default=None):
        if name:
            return self.proxyDict.get(name,default)
        else:
            return self.proxyDict.values()

    def getProxyCache(self,hostname,port,default=None):
        return self.routeCache.get('%s-%s'%(hostname,port),default)

    def __setProxyCache(self,hostname,port,value):
        self.routeCache['%s-%s'%(hostname,port)]=value

    def upProxyPing(self,proxyName,hostname,port,ping,ip):
        proxyDict = self.getProxyCache(hostname,port)
        if proxyDict==None:
            proxyDict = { }
            self.__setProxyCache(hostname,port,proxyDict)

        proxyDict[proxyName]={
                                'tcpping':ping,
                                'proxyName':proxyName,
                                'hitIp':ip
                            }

    def handle(self, sock, addr):
        logging.debug('connection from %s:%s' % addr)

        client = SClient(self,sock,addr)
        try:
            client.handle()
        except:
            logging.exception('client.handle()')
            client.conn.close()

    def close(self):
        logging.info('exit')
        sys.exit(0)

    @staticmethod
    def start_server(port):
        global gfwIP
        gfwIP = {}

        threading.Thread(target=getAddrinfoLoop).start()

        server = SocksServer(('0.0.0.0', port))
        server.addProxy(Socket5Proxy('127.0.0.1',5555))
        gevent.signal(signal.SIGTERM, server.close)
        gevent.signal(signal.SIGINT, server.close)
        logging.info("Server is listening on 0.0.0.0:%d" % port)
        server.serve_forever()

if __name__ == '__main__':
    import sys
    port = 7070
    if len(sys.argv) > 1:
        port = int(sys.argv[1])
    SocksServer.start_server(port)
