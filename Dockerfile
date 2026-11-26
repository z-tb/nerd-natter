FROM python:3.15-slim

# System dependencies.
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        alsa-utils \
        binutils \
        ca-certificates \
        iputils-ping \
        pulseaudio-utils \
        sudo \
        tree \
        vim \
        wget \
        xdotool \
    && rm -rf /var/lib/apt/lists/*

# Supplied by nn-ctl (and the Makefile) to match the host user's UID/GID.
ARG USER_UID
ARG USER_GID
ARG USERNAME=appuser

# Create the container user.
RUN groupadd --gid "${USER_GID}" "${USERNAME}" && \
    useradd \
        --uid "${USER_UID}" \
        --gid "${USER_GID}" \
        --create-home \
        "${USERNAME}" && \
    echo "${USERNAME} ALL=(root) NOPASSWD:ALL" \
        > "/etc/sudoers.d/${USERNAME}" && \
    chmod 0440 "/etc/sudoers.d/${USERNAME}"

# Install Vosk only.
# nerd-dictation itself is not part of the image: its folder is mounted when the
# container starts, so it can live anywhere on the host (setting NERD_DIR).
RUN python3 -m pip install --no-cache-dir vosk

WORKDIR /home/${USERNAME}/nerd-dictation

# Container-side model location (the model folder is mounted here, setting MODEL_DIR).
ENV VOSK_MODEL_DIR=/home/${USERNAME}/.config/nerd-dictation/model

USER ${USERNAME}
