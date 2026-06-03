# 多平台 (amd64 + arm64) image 建置與發佈到 Docker Hub
#
# 發佈前先登入 Docker Hub,並把 IMAGE 設成你的 namespace/repo:
#   docker login
#   make push IMAGE=<dockerhub-user>/sync-gitlab-wiki-to-webopenui TAG=latest

IMAGE     ?= sync-gitlab-wiki-to-webopenui
TAG       ?= latest
PLATFORMS ?= linux/amd64,linux/arm64
BUILDER   ?= wiki-multiarch

.PHONY: builder build push run-local

builder:   ## 建立支援多平台的 buildx builder (僅需一次)
	docker buildx create --name $(BUILDER) --driver docker-container --bootstrap 2>/dev/null || true

build: builder   ## 驗證 amd64 + arm64 都建得起來 (不輸出 image)
	docker buildx build --builder $(BUILDER) --platform $(PLATFORMS) -t $(IMAGE):$(TAG) .

push: builder    ## 多平台建置並推到 Docker Hub (先 docker login;IMAGE 設成你的 namespace/repo)
	docker buildx build --builder $(BUILDER) --platform $(PLATFORMS) \
		-t $(IMAGE):$(TAG) --push .

run-local:       ## 本機單平台建置並跑一次 (用 .env 測試)
	docker build -t $(IMAGE):$(TAG) .
	docker run --rm --env-file .env $(IMAGE):$(TAG)
