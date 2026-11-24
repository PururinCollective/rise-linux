# the RISE banner for an interactive login shell (console and ssh)
case $- in
*i*) [ -z "${RISE_BANNER_SHOWN:-}" ] && [ -x /usr/lib/rise/banner ] && /usr/lib/rise/banner login
     export RISE_BANNER_SHOWN=1 ;;
esac
