CC = gcc
CFLAGS = -Wall -Wextra
LDFLAGS = -lm

SRC = main.c \
      i2c.c \
      iim42652.c


OUT = iim42652

all: $(OUT)

$(OUT): $(SRC)
	$(CC) $(SRC) $(CFLAGS) $(LDFLAGS) -o $(OUT)

clean:
	rm -f $(OUT)

