#!/bin/sh

for var in $(env | grep '_FILE=' | cut -d= -f1); do
    secret_name=$(echo "$var" | sed 's/_FILE//')
    secret_file=$(eval echo \$$var)
    secret_value=$(cat "$secret_file")

    case $secret_file in
        /run/secrets/*)
            export "$secret_name"="$secret_value"
            ;;
    esac
done

exec "$@"
