CC = gcc
CFLAGS = -I dependencies/Fusion -Wall -Wextra
LDFLAGS = -lm

SRC = main.c \
      dependencies/FusionAhrs.c \
      dependencies/FusionBias.c \
      dependencies/FusionCompass.c

OUT = main

all: $(OUT)

$(OUT): $(SRC)
	$(CC) $(SRC) $(CFLAGS) $(LDFLAGS) -o $(OUT)

clean:
	rm -f $(OUT)

