FROM nginx:alpine
COPY index.html data.js /usr/share/nginx/html/
RUN sed -i 's/listen       80;/listen 8080;/' /etc/nginx/conf.d/default.conf
EXPOSE 8080
