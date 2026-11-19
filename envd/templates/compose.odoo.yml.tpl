services:
  odoo:
    image: ${image_tag}
    # OPTIONAL_PORTS
    env_file: .env
    volumes:
      - ${worktree_addons}:/mnt/extra-addons:ro
      - ${worktree_third_party}:/mnt/third-party:ro
      - ${enterprise_dir}:/mnt/enterprise:ro
      - ${data_dir}:/var/lib/odoo
      - ${backups_dir}:/mnt/backups
      - ./odoo.conf:/etc/odoo/odoo.conf:ro
    labels:
      traefik.enable: "true"
      traefik.http.routers.${slug}.rule: Host(`${host_label}.${domain}`)
      traefik.http.routers.${slug}.entrypoints: websecure
      traefik.http.routers.${slug}.tls.certresolver: le
      traefik.http.services.${slug}.loadbalancer.server.port: "8069"
    logging:
      driver: json-file
      options:
        max-size: 10m
        max-file: "3"
    restart: unless-stopped
