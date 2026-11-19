[options]
; Rendered by odoo-envd. NOTE: db connection keys (db_host, db_port, db_user,
; db_password) are intentionally absent — the official odoo docker image
; entrypoint injects them from the HOST/PORT/USER/PASSWORD env vars in .env.
addons_path = /usr/lib/python3/dist-packages/odoo/addons,/mnt/enterprise,/mnt/third-party,/mnt/extra-addons
data_dir = /var/lib/odoo
db_name = ${db_name}
dbfilter = ^${db_name}$$
list_db = False
admin_passwd =
proxy_mode = True
workers = ${workers}
max_cron_threads = ${max_cron_threads}
log_level = info
