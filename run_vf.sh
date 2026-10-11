#!/usr/bin/env bash
cd /mnt/c/Users/juluo/Desktop/eo || exit 1
bash verify_final.sh > /tmp/vf.log 2>&1
rc=$?
echo "exit=$rc"
cat /tmp/vf.log
exit $rc
