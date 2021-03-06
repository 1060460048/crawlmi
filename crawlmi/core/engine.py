from time import time

from twisted.internet import reactor
from twisted.python.failure import Failure

from crawlmi import log, signals
from crawlmi.core.downloader import Downloader
from crawlmi.core.signal_manager import SignalManager
from crawlmi.http import Request, Response
from crawlmi.exceptions import DontStopEngine, StopEngine
from crawlmi.middleware.extension_manager import ExtensionManager
from crawlmi.middleware.pipeline_manager import PipelineManager
from crawlmi.queue import PriorityQueue, MemoryQueue, ResponseQueue
from crawlmi.spider.spider_manager import SpiderManager
from crawlmi.utils.defer import ScheduledCall, defer_fail, defer_succeed, defer_result
from crawlmi.utils.misc import arg_to_iter, load_object


class Engine(object):
    '''
    WARNING: don't stop() and start() engine. Use pause() and unpause(),
    instead.
    '''

    # how many seconds to wait between the checks of response_queue
    QUEUE_CHECK_FREQUENCY = 0.1
    # how often to check is still paused
    PAUSED_CHECK_FREQUENCY = 5
    # how often to check if being idle
    IDLE_CHECK_FREQUENCY = 5

    def __init__(self, settings, project, command_invoked='', clock=None):
        '''Constructor of Engine should be very lightweight, so that things
        can be easily unittested. For any more complicated initialization
        use `setup()`.
        '''
        self.settings = settings
        self.project = project
        self.spiders = SpiderManager(settings)

        self.stop_if_idle = True
        self.initialized = False  # True, when `setup()` has been called
        # name of the command invoking the engine. E.g. `crawl`, `shell`, etc.
        self.command_invoked = command_invoked

        self.spider = None
        self.pending_requests = 0
        self.running = False
        self.paused = False
        # clock is used in unittests
        self.clock = clock or reactor
        self.processing = ScheduledCall(self._process_queue, clock=self.clock)

    def set_spider(self, spider):
        self.spider = spider
        self.settings.spider_settings = spider.spider_settings()

    def setup(self):
        assert self.spider is not None, 'Spider is not set in Engine.'

        # IMPORTANT: order of the following initializations is very important
        # so please, think twice about any changes to it

        # initialize logging
        if self.settings.get_bool('LOG_ENABLED'):
            log.start(
                self.settings['LOG_FILE'],
                self.settings['LOG_LEVEL'],
                self.settings['LOG_STDOUT'],
                self.settings['LOG_ENCODING'])

        # initialize signals
        self.signals = SignalManager(self)

        #initialize stats
        stats_cls = load_object(self.settings.get('STATS_CLASS'))
        self.stats = stats_cls(self)

        # initialize downloader
        self.request_queue = PriorityQueue(lambda _: MemoryQueue())
        self.response_queue = ResponseQueue(
            self.settings.get_int('RESPONSE_ACTIVE_SIZE_LIMIT'))
        self.downloader = Downloader(self.settings, self.request_queue,
                                     self.response_queue, clock=self.clock)

        # initialize extensions
        self.extensions = ExtensionManager(self)
        # initialize downloader pipeline
        self.pipeline = PipelineManager(self)

        self.initialized = True

        # now that everything is ready, set the spider's engine
        self.spider.set_engine(self)

    def crawl_start_requests(self):
        # process start requests from spider
        try:
            requests = self.spider.start_requests()
            for req in arg_to_iter(requests):
                self.download(req)
        except:
            log.err(Failure(), 'Error when processing start requests.')

    def start(self):
        assert self.initialized, 'Engine is not initialized. Call `setup()` to initialize it.'

        self.start_time = time()
        self.running = True
        self.signals.send(signal=signals.engine_started)
        self.processing.schedule(self.QUEUE_CHECK_FREQUENCY)

    def stop(self, reason=''):
        assert self.running, 'Engine is not running.'
        self.running = False

        def _stop(_):
            self.processing.cancel()
            self.downloader.close()
            self.request_queue.close()
            self.response_queue.close()
            log.msg(format='Engine stopped (%(reason)s)', reason=reason)
            self.signals.send(signal=signals.engine_stopped, reason=reason)
            self.stats.dump_stats()

        dfd = defer_succeed(reason, clock=self.clock)
        dfd.addBoth(_stop)
        return dfd

    def pause(self):
        self.paused = True

    def unpause(self):
        self.paused = False

    def download(self, request):
        '''"Download" the given request. First pass it through the downloader
        pipeline.
            - if the request is received, push it to `request_queue`
            - if the response is received , push it to `response_queue`
        '''
        def _success(request_or_response):
            if isinstance(request_or_response, Request):
                self.signals.send(signal=signals.request_received,
                                  request=request_or_response)
                if self.running:
                    self.request_queue.push(request_or_response.priority,
                                            request_or_response)
            elif isinstance(request_or_response, Response):
                request_or_response.request = request
                if self.running:
                    self.response_queue.push(request_or_response)

        def _failure(failure):
            failure.request = request
            dfd = defer_fail(failure, clock=self.clock)
            dfd.addBoth(self._handle_pipeline_result)
            dfd.addBoth(self._finalize_download)
            return dfd

        self.pending_requests += 1
        d = defer_succeed(request, clock=self.clock)
        d.addCallback(self.pipeline.process_request)
        d.addCallbacks(_success, _failure)
        return d

    def is_idle(self):
        return self.pending_requests == 0 and len(self.response_queue) == 0

    def _process_queue(self):
        if not self.running:
            return
        elif self.paused:
            self.processing.schedule(self.PAUSED_CHECK_FREQUENCY)
        elif self.response_queue:
            response = self.response_queue.pop()
            if isinstance(response, Response):
                self.signals.send(signal=signals.response_downloaded,
                                  response=response)
            dfd = defer_result(response, clock=self.clock)
            dfd.addBoth(self.pipeline.process_response)
            dfd.addBoth(self._handle_pipeline_result)
            dfd.addBoth(self._finalize_download)
            dfd.addBoth(lambda _: self.processing.schedule(0))
        elif self.is_idle():
            # send `spider_idle` signal
            res = self.signals.send(signal=signals.spider_idle,
                                    dont_log=DontStopEngine)
            dont_stop = any(isinstance(x, Failure) and
                            isinstance(x.value, DontStopEngine)
                            for _, x in res)
            # more requests have been scheduled
            if not self.is_idle():
                self.processing.schedule(0)
            # slow down a little, but still run
            elif dont_stop or not self.stop_if_idle:
                self.processing.schedule(self.IDLE_CHECK_FREQUENCY)
            else:
                self.stop('finished')
        else:
            self.processing.schedule(self.QUEUE_CHECK_FREQUENCY)

    def _finalize_download(self, _):
        self.pending_requests -= 1

    def _handle_pipeline_result(self, result):
        if result is None:
            pass
        elif isinstance(result, Request):
            self.download(result)
        else:
            assert isinstance(result, (Response, Failure))
            request = result.request
            if isinstance(result, Response):
                flags = ' %s' % result.flags if result.flags else ''
                log.msg(format='Crawled %(url)s [%(status)s]%(flags)s',
                        level=log.DEBUG, url=result.url, status=result.status,
                        flags=flags)
                self.signals.send(signal=signals.response_received,
                                  response=result)
            else:
                self.signals.send(signal=signals.failure_received,
                                  failure=result)
            dfd = defer_result(result, clock=self.clock)
            dfd.addCallbacks(request.callback or self.spider.parse,
                             request.errback)
            dfd.addCallbacks(
                self._handle_spider_output,
                self._handle_spider_error,
                callbackKeywords={'request': request},
                errbackKeywords={'request': request})
            return dfd

    def _handle_spider_output(self, result, request):
        result = arg_to_iter(result)
        for request in result:
            assert isinstance(request, Request), \
                'spider must return None, request or iterable of requests'
            self.download(request)

    def _handle_spider_error(self, failure, request):
        error = failure.value
        if isinstance(error, StopEngine):
            self.stop(error.reason)
            return
        # set `request` in a case the error was raised inside the spider
        failure.request = request
        self.signals.send(signal=signals.spider_error, failure=failure)
        if not getattr(failure.value, 'quiet', False):
            log.err(failure, 'Error when downloading %s' % request)

    def __str__(self):
        return '<%s at 0x%0x>' % (type(self).__name__, id(self))
    __repr__ = __str__
