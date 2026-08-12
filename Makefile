IMAGE := labelshrink
CONTAINER := labelshrink

.PHONY: build start stop restart logs

default:
	start

build:
	docker build -t $(IMAGE) .

start: build
	docker run -d --name $(CONTAINER) --env-file .env --restart unless-stopped $(IMAGE)

stop:
	docker stop $(CONTAINER) || true
	docker rm $(CONTAINER) || true

restart: stop start
