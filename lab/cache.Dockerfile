FROM redis:7.4-alpine
RUN apk add --no-cache iproute2-tc
