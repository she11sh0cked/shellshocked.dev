#!/bin/sh

for var in $(env | grep _FILE= | cut -d= -f1); do
    secret_name=$(echo $var | sed -r "s/_FILE//")
    secret_file=$(eval echo \$$var)
    secret_value=$(cat $secret_file)

    if [[ $secret_file =~ ^/run/secrets/ ]]; then
        export "$secret_name"="$secret_value"
    fi
done

exec "$@"
