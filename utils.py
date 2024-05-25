import os
import logging
import datetime


# Create logger
def get_logger(name=None, log_file='program.log', log_level=logging.INFO):
    # Create a logger
    log_name = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S") if name is None else name
    new_logger = logging.getLogger(log_name)
    new_logger.setLevel(log_level)

    # Create a file handler
    file_handler = logging.FileHandler(log_file)
    file_handler.setLevel(log_level)  # or any level you want

    # Create a stream handler
    stream_handler = logging.StreamHandler()
    stream_handler.setLevel(log_level)

    # Create a formatter
    formatter = logging.Formatter('[%(asctime)s:%(levelname)s] %(name)s: %(message)s')

    # Set the formatter for both handlers
    file_handler.setFormatter(formatter)
    stream_handler.setFormatter(formatter)

    # Add the handlers to the logger
    new_logger.addHandler(file_handler)
    new_logger.addHandler(stream_handler)

    return new_logger


# Timestamp to datetime
def timestamp_to_datetime(timestamp, tz=None):
    if int(timestamp) >= 10000000000:
        ts = int(timestamp) / 1000  # ms to s
    else:
        ts = int(timestamp)

    result_datetime = datetime.datetime.fromtimestamp(ts)

    if tz is not None:
        result_datetime = tz.localize(result_datetime)

    return result_datetime


# Check and make folder
def mk_folder(folder_path):
    if not os.path.exists(folder_path):
        os.makedirs(folder_path)
        print(f"Created folder: {folder_path}")
    else:
        print(f"{folder_path} folder already exists.")