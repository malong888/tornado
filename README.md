# tinytornado backing up just for my own learning
* 包含了tornado的最核心的逻辑：使用epoll注册事件的ioloop，实现socket异步读写和缓存的iostream，实现coroutine的gen文件，管理异常用的上下文管理stackcontext，以及HTTPserver和TCPserver的实现，适合用来学习。
* 代码不到4000行，是原来tornado的五分之一左右，还可以更加精简。
