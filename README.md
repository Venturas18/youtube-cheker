# YouTube Analytics Tool

A comprehensive tool for analyzing YouTube channel performance, generating detailed reports, and visualizing trends.

## Features

- YouTube channel data analysis
- Trend identification and visualization
- Excel report generation
- Customizable channel graphics
- Configuration-driven analysis

## Files Structure

- `main.py` - Main entry point for the application
- `youtube_analyzer.py` - Core YouTube data analysis functionality
- `trends_analyzer.py` - Trend identification and analysis
- `excel_generator.py` - Excel report generation
- `channel_graphics.py` - Channel graphics and visualization
- `config.py` - Configuration settings
- `requirements.txt` - Python dependencies
- `Dockerfile` - Docker container configuration

## Installation

1. Clone the repository
2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

## Configuration

Before running the application, make sure to configure the necessary settings in `config.py`:
- YouTube API credentials
- Channel IDs to analyze
- Report settings
- Output directory paths

## Usage

Run the main application:
```bash
python main.py
```

## Docker Support

Build and run the application using Docker:
```bash
docker build -t youtube-analytics .
docker run youtube-analytics
```

## Dependencies

- Python 3.x
- See `requirements.txt` for detailed Python package dependencies

## License

[Specify your license here]