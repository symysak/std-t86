find . -type f -wholename "*.[ch]" -exec vi -s change-headers.vi \{\} \;
find . -type f -wholename "*.[ch]" -exec dos2unix --safe \{\} \;
find . -type f -wholename "*.[ch]" -exec unix2dos --safe \{\} \;