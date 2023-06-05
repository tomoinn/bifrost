Need to build with buildx

See https://www.docker.com/blog/multi-arch-images/

```
docker buildx create --name mybuilder
docker buildx use mybuilder
docker login
docker buildx build --platform linux/amd64,linux/arm64,linux/arm/v7 -t tomoinn/bifrost:latest --push .
```