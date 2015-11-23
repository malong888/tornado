import tornado.ioloop
import tornado.web
import tornado.gen
import signal

class MainHandler(tornado.web.RequestHandler):
    @tornado.gen.coroutine
    def get(self):
        result = yield self.helloworld()
        self.write(result)

    @tornado.gen.coroutine
    def helloworld(self):
        raise tornado.gen.Return("Hello World!")

def sig_handler(sig, frame):
    tornado.ioloop.IOLoop.instance().stop()
    print "tornado stoped"

application = tornado.web.Application([
    (r"/", MainHandler),
])

if __name__ == "__main__":
    signal.signal(signal.SIGTERM, sig_handler) 
    signal.signal(signal.SIGINT, sig_handler)
    # signal.signal(signal.SIGKILL, sig_handler)

    application.listen(8888)
    tornado.ioloop.IOLoop.instance().start()
